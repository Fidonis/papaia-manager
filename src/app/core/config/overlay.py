"""The customer overlay: the files under `$PAPAIA_CONFIG_DIR/overlay`, the third render layer.

The core merges them last (`render_core.render`: repo base, profile and add-on fragments,
overlay) and never writes them, so this module is their only writer from the manager. Three
rules shape it:

* **Targets are keys, not paths.** The editor offers the structured targets whose overlay
  is merged (a dict is merged, a list appended, a scalar replaced). A request names one by
  key (`librechat`, `litellm`, `searxng`, `model-runner`), so no path from a request is ever
  joined to a directory. `keycloak.conf` and LocalAI's `models.txt` are replaced as a whole by
  an overlay rather than merged, which is too easy to get wrong in a text box, and they are not
  offered. Docker Model Runner's `models.txt` is the exception: it is a list of model names,
  one per line, that the operator is meant to edit; the editor starts from the rendered list
  when there is no overlay yet, so saving never drops models nobody saw, and each line is
  checked the way the core reads it.
* **A write is compare-and-swap.** The revision is the SHA-256 of the file's bytes (empty
  when there is none). It is checked again, under a lock, immediately before the rename, so
  an edit made on the host between page load and Apply is refused instead of overwritten.
* **A file that cannot be parsed is the core's problem too.** `papaia-ctl start` renders
  before it brings anything up, and a broken overlay stops it. The check here is the first of
  two (the core's preview is the second) and keeps such a file from being written.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.i18n import N_, ngettext
from app.i18n import gettext as _

MAX_BYTES = 256 * 1024
DEFAULT_MODE = 0o600

# The core's other render targets (`render_core.BASE_RENDER_TARGETS`). An overlay for one of
# these exists in the core's eyes but is not edited here. `tests/test_config_contract.py`
# compares the two lists with the core's own.
OTHER_RENDER_TARGETS: tuple[str, ...] = (
    "ai/litellm/prometheus.yml",
    "ai/localai/models.txt",
    "ai/localai/models",
    "infra/keycloak/keycloak.conf",
    "infra/keycloak/themes/papaia",
)

# A model reference and an alias, as the core's `model_runner.parse_models` takes them.
_MODEL_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$")
_MODEL_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")

FORMAT_YAML = "yaml"
FORMAT_MODELS = "models"

_LOCK = threading.Lock()


@dataclass(frozen=True)
class OverlayTarget:
    """One file the editor offers."""

    key: str
    label: str
    path: str
    # The Compose profile whose containers read the rendered file. Applying an overlay
    # recreates them (a replaced file is only seen by a new container).
    profile: str
    description: str
    # `yaml` is merged by the core; `models` is a model list that replaces the file whole.
    fmt: str = FORMAT_YAML
    # Further profiles whose containers read what the file renders into (Docker Model
    # Runner's list becomes LiteLLM's model_list), recreated after the target's own.
    also: tuple[str, ...] = ()

    @property
    def profiles(self) -> tuple[str, ...]:
        return (self.profile, *self.also)

    def shipped(self, workspace_dir: str) -> bool:
        """Whether the mounted core renders this target. Only the model list is newer than
        the oldest core this manager serves; the other targets are always there."""
        if self.fmt != FORMAT_MODELS:
            return True
        return Path(workspace_dir, "papaia", "src", self.path).exists()


TARGETS: tuple[OverlayTarget, ...] = (
    OverlayTarget(
        key="librechat",
        label="LibreChat",
        path="ai/librechat/librechat.yaml",
        profile="librechat",
        description=N_("Endpoints, memory and web search settings of the chat interface."),
    ),
    OverlayTarget(
        key="litellm",
        label="LiteLLM",
        path="ai/litellm/config.yaml",
        profile="litellm",
        description=N_("Models and routing of the LLM gateway."),
    ),
    OverlayTarget(
        key="searxng",
        label="SearXNG",
        path="services/searxng/settings.yml",
        profile="librechat-websearch",
        description=N_("Engines and search behaviour of the meta-search."),
    ),
    OverlayTarget(
        key="model-runner",
        label="Docker Model Runner",
        path="ai/model-runner/models.txt",
        profile="model-runner",
        description=N_(
            "The models pulled into Docker Model Runner and offered in LiteLLM, one per line: "
            "a reference and an optional alias."
        ),
        fmt=FORMAT_MODELS,
        also=("litellm",),
    ),
)


def offered(workspace_dir: str) -> tuple[OverlayTarget, ...]:
    """The targets the editor lists for this core."""
    return tuple(t for t in TARGETS if t.shipped(workspace_dir))

_BY_KEY: dict[str, OverlayTarget] = {t.key: t for t in TARGETS}


class OverlayError(Exception):
    """A request the store cannot act on. The message is shown as it is."""


def target(key: str) -> OverlayTarget:
    found = _BY_KEY.get(key)
    if found is None:
        raise OverlayError(_("Unknown overlay file."))
    return found


def overlay_dir(config_dir: str) -> Path:
    return Path(config_dir) / "overlay"


def path_of(config_dir: str, key: str) -> Path:
    return overlay_dir(config_dir) / target(key).path


def revision_of(raw: bytes | None) -> str:
    """SHA-256 of the bytes, or "" for no file (an empty file has a revision of its own)."""
    return "" if raw is None else hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Problem:
    message: str
    line: int | None = None
    column: int | None = None

    def as_json(self) -> dict[str, object]:
        return {"message": self.message, "line": self.line, "column": self.column}


@dataclass(frozen=True)
class OverlayFile:
    """What is on disk for one target."""

    key: str
    exists: bool
    raw: bytes | None
    revision: str
    modified: datetime | None

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8", errors="replace") if self.raw else ""

    @property
    def lines(self) -> int:
        return len(self.text.splitlines()) if self.raw else 0

    @property
    def size(self) -> int:
        return len(self.raw) if self.raw is not None else 0


def starting_text(config_dir: str, workspace_dir: str, target: OverlayTarget) -> str:
    """What the editor starts from for a model list without an overlay: the rendered list, or
    the shipped one before the first render. Empty for a merged target, whose overlay holds
    only what changes."""
    if target.fmt != FORMAT_MODELS:
        return ""
    for path in (Path(config_dir, target.path), Path(workspace_dir, "papaia", "src", target.path)):
        with contextlib.suppress(OSError, UnicodeDecodeError):
            return path.read_text(encoding="utf-8")
    return ""


def read(config_dir: str, key: str) -> OverlayFile:
    """The file as it is now. A symbolic link is refused: writing through it would change
    a file the operator put somewhere else on purpose."""
    path = path_of(config_dir, key)
    if path.is_symlink():
        raise OverlayError(_("This overlay file is a symbolic link. Edit it on the host."))
    try:
        raw = path.read_bytes()
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except FileNotFoundError:
        return OverlayFile(key, False, None, "", None)
    except OSError as exc:
        raise OverlayError(_(
            "The overlay file can't be read (%(value)s)."
        ) % {"value": exc.strerror or exc}) from exc
    return OverlayFile(key, True, raw, revision_of(raw), modified)


def check(content: str, fmt: str = FORMAT_YAML) -> list[Problem]:
    """What is wrong with a draft, as the editor shows it. An empty list means it parses.

    Basic YAML syntax and "the top level is a mapping", nothing more: the render pipeline's
    own merge is the real validator, and the preview runs it. A model list is checked line by
    line, as the core reads it."""
    raw = content.encode("utf-8")
    if len(raw) > MAX_BYTES:
        return [Problem(_("The file is larger than %(value)s KiB.") % {"value": MAX_BYTES // 1024})]
    if "\x00" in content:
        return [Problem(_("The file contains a NUL character."))]
    if fmt == FORMAT_MODELS:
        return _check_models(content)
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or _("invalid YAML")
        if mark is not None:
            return [Problem(str(problem), mark.line + 1, mark.column + 1)]
        return [Problem(str(problem))]
    if document is not None and not isinstance(document, dict):
        return [Problem(_("The top level must be a mapping (key: value)."), 1, 1)]
    return []


def _model_lines(content: str) -> list[tuple[int, list[str]]]:
    return [
        (number, line.split())
        for number, line in enumerate(content.splitlines(), start=1)
        if line.strip() and not line.strip().startswith("#")
    ]


def _check_models(content: str) -> list[Problem]:
    """The lines the core would leave out of the model list, and why."""
    problems: list[Problem] = []
    seen: dict[str, int] = {}
    for number, parts in _model_lines(content):
        reference = parts[0]
        if not _MODEL_REFERENCE_RE.match(reference):
            problems.append(Problem(_("%(value)s is not a model reference.") % {
                "value": reference[:80]}, number, 1))
            continue
        if len(parts) > 2:
            problems.append(Problem(_("Only a reference and an alias are read."), number, 1))
            continue
        alias = parts[1] if len(parts) > 1 else reference.rstrip("/").rsplit("/", 1)[-1]
        if not _MODEL_ALIAS_RE.match(alias):
            problems.append(Problem(_("%(value)s is not a usable model name.") % {
                "value": alias[:80]}, number, 1))
            continue
        if alias in seen:
            problems.append(Problem(_("The name %(name)s is already used on line %(line)s.") % {
                "name": alias, "line": seen[alias]}, number, 1))
            continue
        seen[alias] = number
    return problems


def summary(content: str, fmt: str = FORMAT_YAML) -> str:
    """ "N keys" for a draft that parses, for the line under the editor."""
    if fmt == FORMAT_MODELS:
        count = len(_model_lines(content))
        if not count:
            return _("no models (the module pulls and offers none)")
        return ngettext("%(count)s model", "%(count)s models", count) % {"count": count}
    document = yaml.safe_load(content)
    count = len(document) if isinstance(document, dict) else 0
    if not count:
        return _("empty overlay (changes nothing)")
    return ngettext(
        "top level is a mapping with %(count)s key",
        "top level is a mapping with %(count)s keys",
        count,
    ) % {"count": count}


def _stage(tmp: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, flags, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    with contextlib.suppress(OSError):
        os.chmod(tmp, mode)


def write(config_dir: str, key: str, data: bytes, *, expected_revision: str) -> OverlayFile | None:
    """Replace the file with `data` if it still has the revision the caller read.

    Returns the new state, or None when the file changed in the meantime (nothing written).
    An `OSError` is authoritative and becomes an OverlayError."""
    path = path_of(config_dir, key)
    tmp = path.with_name(f".{path.name}.manager-tmp")
    with _LOCK:
        current = read(config_dir, key)
        if current.revision != expected_revision:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = stat.S_IMODE(path.stat().st_mode) if current.exists else DEFAULT_MODE
            _stage(tmp, data, mode)
            os.replace(tmp, path)
        except OSError as exc:
            raise OverlayError(
                _("The overlay file can't be written (%(value)s).") % {"value": exc.strerror or exc}
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
    return read(config_dir, key)


def delete(config_dir: str, key: str, *, expected_revision: str) -> bool:
    """Remove the file if it still has the revision the caller read. False: it changed."""
    path = path_of(config_dir, key)
    with _LOCK:
        current = read(config_dir, key)
        if current.revision != expected_revision:
            return False
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise OverlayError(
                _("The overlay file can't be removed (%(value)s).") % {"value": exc.strerror or exc}
            ) from exc
    return True


def other_files(config_dir: str) -> list[dict[str, str]]:
    """Files under `overlay/` that this editor does not edit, with the reason.

    A file the core would merge but the editor does not offer is listed as such; one the
    render never looks at (a typo in a path, a leftover) is listed as ignored, which is the
    useful thing to tell an operator who wonders why an edit did nothing."""
    base = overlay_dir(config_dir)
    if not base.is_dir():
        return []
    offered_paths = {t.path for t in TARGETS}
    rendered = set(OTHER_RENDER_TARGETS)
    found: list[dict[str, str]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        rel = path.relative_to(base).as_posix()
        if rel in offered_paths:
            continue
        in_directory = any(rel.startswith(f"{t}/") for t in rendered)
        if rel in rendered or in_directory:
            found.append({"path": rel, "note": _("used by the render, not editable here")})
        else:
            found.append({"path": rel, "note": _("ignored by the render")})
    return found
