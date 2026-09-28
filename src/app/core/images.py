"""Docker images the stack no longer needs.

`papaia-ctl upgrade` pulls the images of the target release through
`docker compose up` and never removes the ones it replaced, so every upgrade
leaves the previous versions of the core stack's and the add-ons' images on the
host. This module finds them and removes them on request -- from the upgrade
runner once an upgrade has succeeded, and from the Upgrade page at any later
time.

What counts as *outdated* is decided by three readings, none of which needs the
previous release's tree:

* **Declared** -- `docker compose config --images`, run the way papaia-ctl runs
  compose: the core file with every override in the config directory, and each
  installed add-on with its own env file. Compose is the only thing that
  resolves `${VAR:-default}` references and the GPU override that swaps the
  LocalAI tag, so reading the YAML here (as `inventory.py` does for profiles
  and labels) would get both wrong.
* **Used** -- the image of every container on the host, stopped ones included.
  That is also what keeps the finished upgrade runner's image (the *previous*
  papaia-manager) out of the list until the runner is dismissed.
* **Local** -- what `docker image ls -a` reports.

An image is outdated when it is neither declared (by tag or by digest) nor used,
and every reference it carries lives in a repository the stack declares.
The second condition is what keeps this from being `docker image prune -a`:
`alpine`, `python`, `docker:cli` and whatever else an operator pulled for
unrelated reasons are never touched. The first is per image rather than per
repository, because different services legitimately pin different versions of
one repository (`postgres:16` next to `postgres:18.3`).

Two properties matter more than completeness:

* **Fail closed.** If the declared images of *any* source cannot be resolved,
  the report carries the reason and no candidate at all. Guessing would delete
  an image the next `start` has to pull again -- or worse, one a stopped add-on
  still needs.
* **Never forced.** Removal goes through `docker image rm` without `-f`, so the
  daemon itself refuses an image a container holds, and a failure on one image
  does not stop the rest.

Known limits: an image of a service that a release drops entirely stays, because
its repository is no longer declared anywhere; and the sizes are the images'
own, so layers shared between two of them are counted twice ("up to"). The size
is what `docker image ls` prints, not what `docker image inspect` reports: with
the containerd image store the latter is the compressed content size, a quarter
of the disk an image actually occupies.

The module doubles as the upgrade runner's entry point --
`python -m app.core.images prune` -- and therefore imports nothing that needs
the manager's settings.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.state import deployment_addons_by_name, load_deployment_yaml

logger = logging.getLogger(__name__)

CORE_SOURCE = "core"

# Names Docker Hub is reachable under. Compose files spell an official image
# `docker.io/library/postgres:18`; the daemon reports the same image as
# `postgres:18`, and the two have to compare equal.
_HUB_HOSTS = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")

_DOCKER_TIMEOUT = 30.0
_COMPOSE_TIMEOUT = 60.0
_REMOVE_TIMEOUT = 120.0

# `docker image inspect` and `docker container inspect` take their ids as argv;
# chunked so a host with hundreds of images stays clear of the argument limit.
_INSPECT_CHUNK = 50

# How many `docker compose config` processes run at once. The core plus a handful
# of add-ons, each a second of YAML work -- enough to overlap, not enough to
# hurt.
_COMPOSE_PARALLELISM = 4

# Serialises removals. Two administrators clicking at once would otherwise both
# compute the same candidates and the second would report a wall of "no such
# image" for what the first already removed.
_prune_lock = asyncio.Lock()

# Prefix of the lines the runner prints. `upgrade.phases_from_log` reads these,
# so they are part of a contract with it -- see `RUNNER_START_MARKER` there.
LOG_PREFIX = "[papaia-manager]"
START_LINE = "Removing outdated Docker images..."
DONE_PREFIX = "Image cleanup "


class ImagesError(Exception):
    """Raised when the images cannot be read or removed."""


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def parse_ref(ref: str) -> tuple[str, str, str]:
    """Split an image reference into `(repository, tag, digest)`.

    The repository is normalised so that a Compose spelling and the daemon's
    spelling of one image are equal. A reference with neither tag nor digest
    means `:latest`, which is what Compose pulls for it.
    """
    value = ref.strip()
    digest = ""
    if "@" in value:
        value, _, digest = value.partition("@")
    tag = ""
    # A colon only separates a tag when it comes after the last slash;
    # `localhost:5000/app` has a port there, not a tag.
    colon = value.rfind(":")
    if colon > value.rfind("/"):
        value, tag = value[:colon], value[colon + 1 :]
    for host in _HUB_HOSTS:
        if value.startswith(host):
            value = value[len(host) :]
            break
    value = value.removeprefix("library/")
    if not tag and not digest:
        tag = "latest"
    return value, tag, digest


def repository_of(ref: str) -> str:
    return parse_ref(ref)[0]


_SIZE_RE = re.compile(r"^([\d.]+)\s*([kKMGT]?B)$")
_UNITS = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}


def parse_size(text: str) -> int:
    """`docker image ls`'s human size (`1.55GB`, `361MB`) as bytes; 0 if unreadable.

    Three significant digits, so the result is an estimate -- which is all a
    "up to X reclaimed" figure claims to be.
    """
    match = _SIZE_RE.match(text.strip())
    if not match:
        return 0
    return int(float(match.group(1)) * _UNITS[match.group(2).upper()])


def format_size(size: int) -> str:
    """Bytes as the decimal units Docker itself prints."""
    value = float(size)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} GB"  # pragma: no cover -- the loop returns at GB


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalImage:
    """One image as the daemon reports it."""

    id: str
    tags: tuple[str, ...] = ()
    digests: tuple[str, ...] = ()
    size: int = 0
    created: str = ""


@dataclass(frozen=True)
class OutdatedImage:
    """An image that can be removed, and what to hand `docker image rm` for it."""

    id: str
    name: str
    remove: tuple[str, ...]
    size: int
    created: str
    source: str

    @property
    def size_human(self) -> str:
        return format_size(self.size)

    @property
    def short_id(self) -> str:
        return self.id.removeprefix("sha256:")[:12]


@dataclass
class ImageReport:
    outdated: list[OutdatedImage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(image.size for image in self.outdated)

    @property
    def total_size_human(self) -> str:
        return format_size(self.total_size)


@dataclass
class PruneResult:
    removed: list[OutdatedImage] = field(default_factory=list)
    failed: list[tuple[OutdatedImage, str]] = field(default_factory=list)
    # Requested ids that are no longer (or never were) candidates.
    skipped: list[str] = field(default_factory=list)

    @property
    def reclaimed(self) -> int:
        return sum(image.size for image in self.removed)


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def build_report(
    declared: dict[str, frozenset[str]],
    local: Iterable[LocalImage],
    used: Collection[str],
    errors: Iterable[str] = (),
) -> ImageReport:
    """Which local images are outdated. Pure -- every input is already read.

    `declared` maps a source (`core`, or an add-on's name) to the references its
    Compose files name, in iteration order of preference: the first source that
    declares a repository is the one an image is attributed to.
    """
    problems = list(errors)
    if problems:
        return ImageReport(errors=problems)

    declared_tags: set[tuple[str, str]] = set()
    declared_digests: set[tuple[str, str]] = set()
    repo_source: dict[str, str] = {}
    for source, refs in declared.items():
        for ref in refs:
            repo, tag, digest = parse_ref(ref)
            repo_source.setdefault(repo, source)
            if digest:
                declared_digests.add((repo, digest))
            # A pin by digest alone has no tag -- `parse_ref` leaves it empty
            # then, rather than inventing `latest`.
            if tag:
                declared_tags.add((repo, tag))

    outdated: list[OutdatedImage] = []
    for image in local:
        if image.id in used:
            continue
        tags = [parse_ref(t) for t in image.tags]
        digests = [parse_ref(d) for d in image.digests]
        if any((repo, tag) in declared_tags for repo, tag, _ in tags):
            continue
        if any((repo, digest) in declared_digests for repo, _, digest in digests):
            continue
        repos = {repo for repo, _, _ in tags} | {repo for repo, _, _ in digests}
        # No reference at all: a local build, or the remains of one. Nothing ties
        # it to the stack, so it is not this module's to remove.
        if not repos or not repos <= repo_source.keys():
            continue
        if tags:
            name = ", ".join(f"{repo}:{tag}" for repo, tag, _ in tags)
            remove = tuple(f"{repo}:{tag}" for repo, tag, _ in tags)
        else:
            repo, _, digest = digests[0]
            name = f"{repo}@{digest[:19]}"
            remove = (image.id,)
        outdated.append(
            OutdatedImage(
                id=image.id,
                name=name,
                remove=remove,
                size=image.size,
                created=image.created,
                source=repo_source[sorted(repos)[0]],
            )
        )
    outdated.sort(key=lambda i: (i.source != CORE_SOURCE, i.source, i.name))
    return ImageReport(outdated=outdated)


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------


async def _run(args: list[str], *, limit: float) -> str:
    """Run a docker command and return stdout, raising `ImagesError` on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError) as exc:
        raise ImagesError(f"cannot invoke docker: {exc}") from exc
    try:
        async with asyncio.timeout(limit):
            raw_out, raw_err = await proc.communicate()
    except TimeoutError as exc:
        proc.kill()
        raise ImagesError(f"{' '.join(args[:3])} timed out after {limit:.0f}s") from exc
    if proc.returncode != 0:
        detail = raw_err.decode(errors="replace").strip() or f"exit code {proc.returncode}"
        raise ImagesError(detail)
    return raw_out.decode(errors="replace")


def _chunks(items: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(items), _INSPECT_CHUNK):
        yield items[start : start + _INSPECT_CHUNK]


async def local_images() -> list[LocalImage]:
    # One row per tag, so an id can repeat. The listing is where the *displayed*
    # size comes from -- see the module docstring -- and the inspect below is
    # where the references come from.
    listing = await _run(
        ["docker", "image", "ls", "--all", "--no-trunc", "--format", "{{json .}}"],
        limit=_DOCKER_TIMEOUT,
    )
    sizes: dict[str, int] = {}
    for row in listing.splitlines():
        if not row.strip():
            continue
        try:
            entry = json.loads(row)
        except json.JSONDecodeError as exc:
            raise ImagesError(f"docker image ls gave unreadable output: {exc}") from exc
        sizes[str(entry.get("ID", ""))] = parse_size(str(entry.get("Size", "")))
    ids = sorted(i for i in sizes if i)
    images: list[LocalImage] = []
    for chunk in _chunks(ids):
        output = await _run(
            ["docker", "image", "inspect", "--format", "{{json .}}", *chunk],
            limit=_DOCKER_TIMEOUT,
        )
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ImagesError(f"docker image inspect gave unreadable output: {exc}") from exc
            images.append(_local_image(entry, sizes.get(str(entry.get("Id", "")), 0)))
    return images


def _local_image(entry: dict[str, Any], listed_size: int = 0) -> LocalImage:
    """Split what the daemon calls tags into real tags and digest references.

    With the containerd image store, an image that was pulled by digest lists
    `repo@sha256:...` among its `RepoTags`. Left there it would parse as a tag
    with no name and turn up in the removal command as `repo:`.
    """
    names = [str(t) for t in entry.get("RepoTags") or [] if t and "<none>" not in t]
    digests = [str(d) for d in entry.get("RepoDigests") or [] if d and "<none>" not in d]
    digests += [n for n in names if "@" in n and n not in digests]
    return LocalImage(
        id=str(entry.get("Id", "")),
        tags=tuple(n for n in names if "@" not in n),
        digests=tuple(digests),
        size=listed_size or int(entry.get("Size") or 0),
        created=str(entry.get("Created") or ""),
    )


async def used_image_ids() -> set[str]:
    """The image id of every container on the host, running or not."""
    listing = await _run(
        ["docker", "ps", "--all", "--quiet", "--no-trunc"], limit=_DOCKER_TIMEOUT
    )
    containers = [line.strip() for line in listing.splitlines() if line.strip()]
    used: set[str] = set()
    for chunk in _chunks(containers):
        output = await _run(
            ["docker", "container", "inspect", "--format", "{{.Image}}", *chunk],
            limit=_DOCKER_TIMEOUT,
        )
        used.update(line.strip() for line in output.splitlines() if line.strip())
    return used


# ---------------------------------------------------------------------------
# Declared images
# ---------------------------------------------------------------------------


def _core_command(config_dir: str, workspace_dir: str) -> list[str]:
    """`docker compose config --images` for the core, as `papaia-ctl start` runs it.

    Every override in the config directory is included, unlike `start`, which
    skips those whose external networks do not exist yet: `config` never looks
    at networks, and the override that matters here -- the LocalAI GPU variant --
    changes an image, not a network.
    """
    root = Path(workspace_dir) / "papaia" / "src"
    args = ["docker", "compose", "-f", str(root / "docker-compose.yml")]
    overrides = Path(config_dir) / "overrides"
    if overrides.is_dir():
        for override in sorted(overrides.glob("docker-compose.*.override.yml")):
            args += ["-f", str(override)]
    args += ["--env-file", str(root / ".env"), "config", "--images"]
    return args


def _addon_command(path: Path, workspace_dir: str) -> list[str]:
    """The same for one add-on, as `_addon_compose` in the core's `addon.sh` runs it.

    The project name is pinned to the directory name for the reason given there:
    the root `.env` carries the core's `COMPOSE_PROJECT_NAME`, and an add-on's
    own env file has to be able to override what it declares.
    """
    root_env = Path(workspace_dir) / "papaia" / "src" / ".env"
    args = ["docker", "compose", "-p", path.name, "-f", str(path / "docker-compose.yml")]
    if root_env.is_file():
        args += ["--env-file", str(root_env)]
    if (path / ".env").is_file():
        args += ["--env-file", str(path / ".env")]
    return [*args, "config", "--images"]


def _addon_paths(config_dir: str, workspace_dir: str) -> dict[str, Path]:
    """Every installed add-on's directory -- active or not.

    An inactive add-on keeps its images: `addon start` would otherwise pull them
    again, and this module must never be the reason it has to.
    """
    deployment = load_deployment_yaml(config_dir)
    paths: dict[str, Path] = {}
    for name, entry in deployment_addons_by_name(deployment).items():
        raw = str(entry.get("path", ""))
        if not raw:
            continue
        path = Path(raw)
        paths[name] = path if path.is_absolute() else Path(workspace_dir) / path
    return paths


async def declared_images(
    config_dir: str, workspace_dir: str
) -> tuple[dict[str, frozenset[str]], list[str]]:
    """References the deployment declares, by source, and why any could not be read."""
    commands: dict[str, list[str]] = {}
    errors: list[str] = []

    if not (Path(workspace_dir) / "papaia" / "src" / "docker-compose.yml").is_file():
        return {}, ["the papaia checkout has no src/docker-compose.yml"]
    commands[CORE_SOURCE] = _core_command(config_dir, workspace_dir)

    try:
        addons = _addon_paths(config_dir, workspace_dir)
    except (OSError, yaml.YAMLError) as exc:
        return {}, [f"deployment.yaml could not be read: {exc}"]
    for name, path in sorted(addons.items()):
        if not (path / "docker-compose.yml").is_file():
            errors.append(f"add-on {name}: {path}/docker-compose.yml does not exist")
            continue
        commands[name] = _addon_command(path, workspace_dir)

    gate = asyncio.Semaphore(_COMPOSE_PARALLELISM)

    async def resolve(source: str) -> tuple[str, frozenset[str] | str]:
        async with gate:
            try:
                out = await _run(commands[source], limit=_COMPOSE_TIMEOUT)
            except ImagesError as exc:
                return source, str(exc)
        return source, frozenset(line.strip() for line in out.splitlines() if line.strip())

    declared: dict[str, frozenset[str]] = {}
    for source, outcome in await asyncio.gather(*(resolve(s) for s in commands)):
        if isinstance(outcome, str):
            errors.append(f"{source}: docker compose config failed: {outcome}")
        else:
            declared[source] = outcome
    # Core first, the rest by name: `build_report` attributes an image to the
    # first source that declares its repository.
    order = sorted(declared, key=lambda source: (source != CORE_SOURCE, source))
    return {source: declared[source] for source in order}, errors


# ---------------------------------------------------------------------------
# Report and removal
# ---------------------------------------------------------------------------


async def gather_report(config_dir: str, workspace_dir: str) -> ImageReport:
    """Read the three inputs and reach the verdict. Never raises for a Docker fault.

    An unreachable daemon is a state this reports rather than an error it raises,
    for the reason every polled view here does: the panel is recreated by the
    operation it is describing.
    """
    try:
        (declared, errors), local, used = await asyncio.gather(
            declared_images(config_dir, workspace_dir), local_images(), used_image_ids()
        )
    except ImagesError as exc:
        return ImageReport(errors=[str(exc)])
    return build_report(declared, local, used, errors)


async def prune(
    config_dir: str,
    workspace_dir: str,
    *,
    only: Collection[str] | None = None,
) -> PruneResult:
    """Remove the outdated images -- all of them, or only those named by id.

    The candidates are recomputed here, under the lock, and `only` merely
    narrows them: an id a client sends that is not a candidate right now is
    reported as skipped and never removed. What may be deleted is decided by
    the verdict above, not by the request.
    """
    async with _prune_lock:
        report = await gather_report(config_dir, workspace_dir)
        if report.errors:
            raise ImagesError("; ".join(report.errors))
        by_id = {image.id: image for image in report.outdated}
        wanted = list(by_id) if only is None else [i for i in by_id if i in only]
        result = PruneResult(skipped=[] if only is None else sorted(set(only) - by_id.keys()))
        for image_id in wanted:
            image = by_id[image_id]
            error = await _remove(image)
            if error:
                result.failed.append((image, error))
            else:
                result.removed.append(image)
        return result


async def _remove(image: OutdatedImage) -> str:
    """Remove one image by every reference it carries. Returns "" or the reason."""
    for ref in image.remove:
        try:
            await _run(["docker", "image", "rm", ref], limit=_REMOVE_TIMEOUT)
        except ImagesError as exc:
            return str(exc)
    return ""


# ---------------------------------------------------------------------------
# The upgrade runner's entry point
# ---------------------------------------------------------------------------


def _say(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


async def _run_prune(config_dir: str, workspace_dir: str) -> None:
    _say(START_LINE)
    try:
        result = await prune(config_dir, workspace_dir)
    except ImagesError as exc:
        # Reported, not raised: the upgrade has already succeeded, and a
        # cleanup that could not run must not read as a failure of it.
        _say(f"{DONE_PREFIX}skipped -- nothing was removed: {exc}")
        return
    for image in result.removed:
        _say(f"  removed {image.name} ({image.size_human})")
    for image, reason in result.failed:
        _say(f"  kept {image.name}: {reason}")
    _say(
        f"{DONE_PREFIX}finished: {len(result.removed)} removed, "
        f"up to {format_size(result.reclaimed)} reclaimed"
        + (f", {len(result.failed)} kept" if result.failed else "")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.core.images")
    sub = parser.add_subparsers(dest="command", required=True)
    prune_parser = sub.add_parser("prune", help="remove outdated stack images")
    prune_parser.add_argument("--config-dir", required=True)
    prune_parser.add_argument("--workspace-dir", required=True)
    args = parser.parse_args(argv)
    asyncio.run(_run_prune(args.config_dir, args.workspace_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
