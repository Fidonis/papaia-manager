"""The module catalog: the suite's modules with their versions and their state here.

Two sources, both read-only:

* The suite bill of materials of the core checkout, `papaia/compat/suite-<VERSION>.yaml`
  (schema `papaia.suite-bom/1`). Its repos with role `module` are the modules the suite ships,
  in the version shown (PLT-31-A1). When the core pins a release key
  (`compat/release-key.pub`) the file must carry a valid ECDSA P-256 signature
  (`suite-<VERSION>.yaml.sig`, as `cosign sign-blob` writes it) or it is refused; without a
  pinned key the versions are shown as not verified.
* The platform catalog `platform/services.json` of the config directory, read with the kit's
  catalog reader (schema check, entry hashes, last valid state on a broken file). A module
  with an entry is installed; its user interface entry says whether it runs and where its
  console is. A module of the suite without an entry is "available": it is installed from the
  catalog with `papaia-ctl addon install`, without copying anything out of a module repository.

Nothing here grants a right. The link is the module's own address from the catalog; the
module checks every call itself.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from papaia_kit.catalog import Catalog, Entry, ServicesReader

from app.i18n import current_locale

MAX_BOM_BYTES = 1024 * 1024
_MAX_SIGNATURE_BYTES = 4096
_MAX_KEY_BYTES = 16 * 1024
_MAX_REPOS = 64

_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]{1,64})?")
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_ROLES = frozenset({"core", "manager", "module", "addon", "mcp", "kit", "registry"})
_BOM_FIELDS = frozenset(
    {"schema", "suite", "scope", "created", "contract_generation", "signing_key", "repos",
     "pairings", "totals"}
)
_BOM_REQUIRED = frozenset(
    {"schema", "suite", "scope", "created", "contract_generation", "signing_key", "repos",
     "totals"}
)
_REPO_FIELDS = frozenset(
    {"name", "role", "version", "tag", "commit", "containers", "addon_api", "min_core",
     "schema_version", "snapshot_sha256", "images"}
)

# Names of the modules when the catalog has no entry of theirs to take the title from.
_TITLES: Mapping[str, Mapping[str, str]] = {
    "papaia-studio": {"de": "Studio", "en": "Studio"},
    "papaia-data": {"de": "Data", "en": "Data"},
    "papaia-flow": {"de": "Flow", "en": "Flow"},
    "papaia-assist": {"de": "Assistant", "en": "Assistant"},
}

State = Literal["available", "installed", "running", "stopped"]
BomError = Literal["missing", "invalid", "signature"]


@dataclass(frozen=True)
class BomStatus:
    suite: str | None
    verified: bool
    error: BomError | None


@dataclass(frozen=True)
class Module:
    name: str
    title: Mapping[str, str]
    description: Mapping[str, str]
    version: str
    installed_version: str | None
    state: State
    url: str | None


@dataclass(frozen=True)
class ModuleCatalog:
    bom: BomStatus
    modules: tuple[Module, ...] = field(default=())
    catalog_error: str | None = None


class _RefusedError(Exception):
    def __init__(self, reason: BomError) -> None:
        super().__init__(reason)
        self.reason: BomError = reason


def _read_file(path: Path, limit: int) -> bytes | None:
    """A regular file of at most `limit` bytes, or None when there is none."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _RefusedError("invalid")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise _RefusedError("invalid")
    return data


def _verify(raw: bytes, signature_file: bytes | None, key_file: bytes) -> None:
    if signature_file is None:
        raise _RefusedError("signature")
    try:
        key = serialization.load_pem_public_key(key_file)
        signature = base64.b64decode(b"".join(signature_file.split()), validate=True)
    except (ValueError, binascii.Error, TypeError) as exc:
        raise _RefusedError("signature") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise _RefusedError("signature")
    try:
        key.verify(signature, raw, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise _RefusedError("signature") from exc


def _modules_of(raw: bytes, suite: str) -> tuple[tuple[str, str], ...]:
    """(name, version) of every repo with role `module`, after a structural check."""
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise _RefusedError("invalid") from exc
    if (
        not isinstance(document, dict)
        or not _BOM_REQUIRED <= document.keys() <= _BOM_FIELDS
        or document["schema"] != "papaia.suite-bom/1"
        or document["suite"] != suite
        or not isinstance(document["repos"], list)
        or not 1 <= len(document["repos"]) <= _MAX_REPOS
    ):
        raise _RefusedError("invalid")
    modules: dict[str, str] = {}
    for repo in document["repos"]:
        if not isinstance(repo, dict) or not {"name", "role", "version"} <= repo.keys():
            raise _RefusedError("invalid")
        name, role, version = repo["name"], repo["role"], repo["version"]
        if (
            not repo.keys() <= _REPO_FIELDS
            or not isinstance(name, str) or not _NAME.fullmatch(name)
            or role not in _ROLES
            or not isinstance(version, str) or not _VERSION.fullmatch(version)
        ):
            raise _RefusedError("invalid")
        if role == "module":
            if name in modules:
                raise _RefusedError("invalid")
            modules[name] = version
    return tuple(modules.items())


def read_bom(workspace_dir: str) -> tuple[BomStatus, tuple[tuple[str, str], ...]]:
    compat = Path(workspace_dir) / "papaia" / "compat"
    try:
        version_file = _read_file(Path(workspace_dir) / "papaia" / "VERSION", 64)
        suite = version_file.decode("ascii").strip() if version_file is not None else ""
        if not _VERSION.fullmatch(suite):
            return BomStatus(suite=None, verified=False, error="missing"), ()
        raw = _read_file(compat / f"suite-{suite}.yaml", MAX_BOM_BYTES)
        if raw is None:
            return BomStatus(suite=suite, verified=False, error="missing"), ()
        key = _read_file(compat / "release-key.pub", _MAX_KEY_BYTES)
        if key is not None:
            _verify(raw, _read_file(compat / f"suite-{suite}.yaml.sig", _MAX_SIGNATURE_BYTES), key)
        modules = _modules_of(raw, suite)
    except UnicodeDecodeError:
        return BomStatus(suite=None, verified=False, error="missing"), ()
    except _RefusedError as refused:
        return BomStatus(suite=suite, verified=False, error=refused.reason), ()
    return BomStatus(suite=suite, verified=key is not None, error=None), modules


# -- the platform catalog ----------------------------------------------------------------

_readers: dict[Path, ServicesReader] = {}
_readers_lock = threading.Lock()


def reset_readers() -> None:
    """Forget the cached readers (their last valid state); for tests."""
    with _readers_lock:
        _readers.clear()


def _reader(config_dir: str) -> ServicesReader:
    path = Path(config_dir) / "platform" / "services.json"
    with _readers_lock:
        reader = _readers.get(path)
        if reader is None:
            reader = ServicesReader(path)
            _readers[path] = reader
        return reader


def platform_catalog(config_dir: str) -> Catalog | None:
    """The last valid platform catalog, or None while there is none (no platform, or broken)."""
    return _reader(config_dir).get()


def _text(value: Any) -> Mapping[str, str]:
    if isinstance(value, Mapping):
        return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}
    return {}


def _module(name: str, version: str, catalog: Catalog | None) -> Module:
    entries: list[Entry] = []
    if catalog is not None:
        entries = sorted(
            (e for e in catalog.entries.values() if e.provider == name), key=lambda e: e.id
        )
    ui = next((e for e in entries if e.kind == "ui"), None)
    shown = ui or (entries[0] if entries else None)
    state: State = "available"
    url: str | None = None
    if shown is not None:
        state = "installed"
        if ui is not None and ui.state is not None:
            state = "running" if ui.state.value == "running" else "stopped"
        if state == "running" and catalog is not None and ui is not None:
            surfaces = sorted(
                (s for s in catalog.surfaces if s.entry_id == ui.id), key=lambda s: (s.order, s.id)
            )
            url = surfaces[0].public_url if surfaces else None
    title = _text(shown.data.get("title")) if shown is not None else {}
    return Module(
        name=name,
        title=title or _TITLES.get(name, {"de": name, "en": name}),
        description=_text(shown.data.get("description")) if shown is not None else {},
        version=version,
        installed_version=shown.version if shown is not None else None,
        state=state,
        url=url,
    )


def read_catalog(workspace_dir: str, config_dir: str, *, refresh: bool = False) -> ModuleCatalog:
    bom, modules = read_bom(workspace_dir)
    reader = _reader(config_dir)
    catalog = reader.refresh() if refresh else reader.get()
    return ModuleCatalog(
        bom=bom,
        modules=tuple(_module(name, version, catalog) for name, version in modules),
        catalog_error=reader.status.error,
    )


def localized(text: Mapping[str, str]) -> str:
    """The text in the language of the request, else English, else any."""
    language = current_locale().split("_")[0].split("-")[0]
    return text.get(language) or text.get("en") or next(iter(text.values()), "")


def module_json(module: Module) -> dict[str, Any]:
    return {
        "name": module.name,
        "title": localized(module.title),
        "description": localized(module.description),
        "version": module.version,
        "installed_version": module.installed_version,
        "state": module.state,
        "url": module.url,
    }
