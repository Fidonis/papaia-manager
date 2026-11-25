"""A stand-in `docker` command for the test world, never a real Docker.

`tests/design_serve.py` puts the directory of its launcher (`tests/fake_docker_bin/docker`)
first on the `PATH` of the manager it serves, so every page that runs `docker` -- the image
report of the Upgrade page through `app.core.images`, the container calls of
`app.core.runner` -- asks this script instead of the Docker daemon of whatever host runs the
tests. It answers the calls of the image report from a fixed synthetic image set and every
other call like an unreachable daemon: exit 1 and `Cannot connect to the Docker daemon` on
stderr. It starts no process and opens no socket.

Answered:
  docker image ls --all --no-trunc --format {{json .}}
  docker image inspect --format {{json .}} ID...
  docker ps --all --quiet --no-trunc
  docker container inspect --format {{.Image}} ID...
  docker compose ... config --images

Environment:
  PAPAIA_FAKE_DOCKER_LOG     file each call's argv (and the pid that served it) is appended to,
                             one JSON object per line
  PAPAIA_FAKE_DOCKER_DELAY   seconds to wait before answering any call (a slow host)
  PAPAIA_FAKE_DOCKER_IMAGES  serve a large synthetic set of this many images instead of the
                             small one (a host with many images)

The small set is the default and fast: three images the core still declares, three it no
longer declares (the outdated ones), one only a stopped container holds and one of an
unrelated repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_LOG = "PAPAIA_FAKE_DOCKER_LOG"
ENV_DELAY = "PAPAIA_FAKE_DOCKER_DELAY"
ENV_IMAGES = "PAPAIA_FAKE_DOCKER_IMAGES"

UNREACHABLE = (
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?"
)

# What `docker compose config --images` prints for the core of the test world.
DECLARED = (
    "docker.io/library/postgres:18.3",
    "docker.io/valkey/valkey:9-alpine",
    "ghcr.io/example/papaia-sample:2.0",
)

# The largest set the stand-in serves; keeps a typo in the environment from filling memory.
_MAX_IMAGES = 20000


@dataclass(frozen=True)
class Image:
    id: str
    repository: str
    tag: str
    size: str
    created: str = "2026-10-01T08:00:00Z"

    @property
    def ref(self) -> str:
        return f"{self.repository}:{self.tag}"


def _id(seed: str, kind: str = "sha256:") -> str:
    return kind + hashlib.sha256(seed.encode()).hexdigest()


def small_set() -> tuple[list[Image], dict[str, str]]:
    """The default images, and the containers (id to image id) that hold some of them."""
    images = [
        Image(_id("postgres-18.3"), "postgres", "18.3", "456MB"),
        Image(_id("postgres-16"), "postgres", "16", "432MB"),
        Image(_id("valkey-9"), "valkey/valkey", "9-alpine", "61.2MB"),
        Image(_id("valkey-8"), "valkey/valkey", "8-alpine", "58.9MB"),
        Image(_id("valkey-7"), "valkey/valkey", "7-alpine", "41.5MB"),
        Image(_id("sample-2.0"), "ghcr.io/example/papaia-sample", "2.0", "1.21GB"),
        Image(_id("sample-1.9"), "ghcr.io/example/papaia-sample", "1.9", "1.19GB"),
        Image(_id("alpine-3.20"), "alpine", "3.20", "8.83MB"),
    ]
    by_name = {image.ref: image.id for image in images}
    containers = {
        _id("container-postgres", ""): by_name["postgres:18.3"],
        # A stopped container still holds the previous valkey: it is kept.
        _id("container-valkey-old", ""): by_name["valkey/valkey:7-alpine"],
    }
    return images, containers


def large_set(count: int) -> tuple[list[Image], dict[str, str]]:
    """`count` images: the declared ones, then old tags of the declared repositories and of an
    unrelated one, every tenth held by a container."""
    repositories = ("postgres", "valkey/valkey", "ghcr.io/example/papaia-sample", "alpine")
    declared = ("18.3", "9-alpine", "2.0")
    images: list[Image] = []
    for index in range(count):
        repository = repositories[index % len(repositories)]
        tag = declared[index] if index < len(declared) else f"old-{index}"
        images.append(Image(_id(f"image-{index}"), repository, tag, f"{100 + index % 900}MB"))
    containers = {
        _id(f"container-{index}", ""): images[index].id for index in range(0, count, 10)
    }
    return images, containers


def current_set(environ: Mapping[str, str]) -> tuple[list[Image], dict[str, str]]:
    raw = environ.get(ENV_IMAGES, "").strip()
    if raw.isdigit() and 0 < int(raw) <= _MAX_IMAGES:
        return large_set(int(raw))
    return small_set()


def _listed(image: Image) -> str:
    return json.dumps(
        {
            "Containers": "N/A",
            "CreatedAt": image.created,
            "ID": image.id,
            "Repository": image.repository,
            "Tag": image.tag,
            "Size": image.size,
        }
    )


def _inspected(image: Image) -> str:
    return json.dumps(
        {
            "Id": image.id,
            "RepoTags": [image.ref],
            "RepoDigests": [],
            "Size": 1,
            "Created": image.created,
        }
    )


def answer(argv: Sequence[str], environ: Mapping[str, str]) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of `docker <argv>`."""
    args = list(argv)
    images, containers = current_set(environ)
    by_id = {image.id: image for image in images}
    if args == ["image", "ls", "--all", "--no-trunc", "--format", "{{json .}}"]:
        return 0, "".join(_listed(image) + "\n" for image in images), ""
    if args[:4] == ["image", "inspect", "--format", "{{json .}}"] and len(args) > 4:  # noqa: PLR2004
        missing = [i for i in args[4:] if i not in by_id]
        if missing:
            return 1, "", f"Error response from daemon: No such image: {missing[0]}\n"
        return 0, "".join(_inspected(by_id[i]) + "\n" for i in args[4:]), ""
    if args == ["ps", "--all", "--quiet", "--no-trunc"]:
        return 0, "".join(c + "\n" for c in containers), ""
    if args[:4] == ["container", "inspect", "--format", "{{.Image}}"] and len(args) > 4:  # noqa: PLR2004
        missing = [c for c in args[4:] if c not in containers]
        if missing:
            return 1, "", f"Error response from daemon: No such container: {missing[0]}\n"
        return 0, "".join(containers[c] + "\n" for c in args[4:]), ""
    if args[:1] == ["compose"] and args[-2:] == ["config", "--images"]:
        return 0, "".join(ref + "\n" for ref in DECLARED), ""
    return 1, "", UNREACHABLE + "\n"


def write_slow_host_docker(directory: Path, *, seconds: int = 60) -> Path:
    """A `docker` in `directory` that stands in for a slow host: it notes each call in
    `host-docker.log` next to it and sleeps `seconds` before it exits. Put first on the PATH a
    server inherits, it shows whether anything reached the host's docker."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "docker"
    script.write_text(
        "#!/bin/sh\n"
        "# A host whose docker does not answer in time (tests/fake_docker.py).\n"
        'echo "$*" >> "$(dirname "$0")/host-docker.log"\n'
        f"exec sleep {int(seconds)}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def read_log(path: Path) -> list[dict[str, Any]]:
    """The calls the stand-in logged, oldest first; empty when none was made."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _log(argv: Sequence[str], path: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "argv": list(argv)}) + "\n")


def _delay(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value if 0 < value <= 600 else 0.0  # noqa: PLR2004


def main(argv: Sequence[str]) -> int:
    log = os.environ.get(ENV_LOG, "")
    if log:
        _log(argv, log)
    delay = _delay(os.environ.get(ENV_DELAY, ""))
    if delay:
        time.sleep(delay)
    code, out, err = answer(argv, os.environ)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
