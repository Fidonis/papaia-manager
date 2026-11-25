"""Serve the manager in the stand-in world for the kit's browser checks (scripts/design-check.sh).

Binds 127.0.0.1 only, on an unprivileged port, with a session secret made for this run, and
writes one Playwright storage state per test person of the page list that signs that person
in (tests/design_world.py): Anna for the administration, Emil for the inventory. The manager
talks to no real service: Keycloak is the simulation of the tests, and pages that would ask
LiteLLM or Qdrant show their "unreachable" state. Docker is the stand-in command of
tests/fake_docker.py: its directory (tests/fake_docker_bin) goes first on the PATH of this
process before the application starts, so every `docker` the manager runs -- the image report
of the Upgrade page, the container calls of the runner -- reaches the stand-in and never the
Docker daemon of the host. The stand-in answers the image report from a small synthetic image
set (the Upgrade page lists its outdated images) and every other call like an unreachable
daemon. Each call is logged to PAPAIA_FAKE_DOCKER_LOG (default: docker.log in the world's
temporary directory). A test tool, never part of the image.

Usage (from the repository root):
  uv run --project src python tests/design_serve.py --port PORT \
      --storage-state [PERSON=]FILE [--storage-state PERSON=FILE ...] [--rag-catalog]
  uv run --project src python tests/design_serve.py --port PORT --storage-state FILE \
      [--mail-relay] [--with-backups]
  uv run --project src python tests/design_serve.py --list-persons
  uv run --project src python tests/design_serve.py --list-pages [--person PERSON]
A FILE without a person is Anna's. --mail-relay serves a core with the mail relay switched on
and set up (design_world.add_mail_relay), for the browser check of the Mail page.
--with-backups adds the restore points of design_world.BACKUPS (the browser path of the
backup page, tests/backup_browser.mjs).
--with-ingest puts the fake ingester of tests/design_ingest.py behind the manager (the browser
path of the ingest lists, tests/ingest_browser.mjs).
--docker-delay SECONDS makes the stand-in wait that long before each answer (a slow host);
--docker-images COUNT serves a synthetic set of that many images (a host with many images).
Both default to the small, fast set.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
from collections.abc import MutableMapping, Sequence
from pathlib import Path

import yaml

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
SRC = REPO / "src"
sys.path.insert(0, str(TESTS))

import design_world  # noqa: E402

PAGES_FILE = REPO / "ui" / "pages.yaml"
FAKE_DOCKER_BIN = TESTS / "fake_docker_bin"


def _rows(pages_file: Path) -> list[dict[str, str]]:
    doc = yaml.safe_load(pages_file.read_text(encoding="utf-8"))
    return [row for row in doc["pages"] if "path" in row]


def design_pages(pages_file: Path = PAGES_FILE, person: str | None = None) -> list[str]:
    """The pages of the list with a path (of `person`, else all), in order, placeholders filled."""
    return [
        design_world.sample_path(row["path"])
        for row in _rows(pages_file)
        if person is None or row["person"] == person
    ]


def design_persons(pages_file: Path = PAGES_FILE) -> list[str]:
    """The test persons who see a page of the list, in order of their first page."""
    return list(dict.fromkeys(row["person"] for row in _rows(pages_file)))


def write_storage_state(
    path: Path, *, port: int, secret: str, person: str = "anna", username: str | None = None
) -> None:
    """A Playwright storage state with the session cookie of `person` for 127.0.0.1:<port>;
    `username` is the name the frame shows (design_world.session_cookie)."""
    cookie = {
        "name": design_world.SESSION_COOKIE,
        "value": design_world.session_cookie(
            secret, roles=design_world.PERSONS[person], person=person, username=username
        ),
        "domain": "127.0.0.1",
        "path": "/",
        "expires": -1,
        # Plain http on the loopback: the cookie cannot be marked secure.
        "httpOnly": True,
        "secure": False,
        "sameSite": "Lax",
    }
    path.write_text(json.dumps({"cookies": [cookie], "origins": []}), encoding="utf-8")


def use_fake_docker(
    environ: MutableMapping[str, str],
    *,
    log: Path,
    delay: float = 0.0,
    images: int = 0,
) -> None:
    """Make `docker` in `environ` the stand-in of tests/fake_docker.py, whatever PATH held.

    The stand-in's directory goes first on PATH, so a `docker` of the host (or of a test that
    stands in for a slow host) is never reached. The interpreter is this one; a log the caller
    already named is kept.
    """
    rest = environ.get("PATH", "")
    environ["PATH"] = os.pathsep.join(p for p in (str(FAKE_DOCKER_BIN), rest) if p)
    environ["PAPAIA_FAKE_DOCKER_PYTHON"] = sys.executable
    environ.setdefault("PAPAIA_FAKE_DOCKER_LOG", str(log))
    if delay:
        environ["PAPAIA_FAKE_DOCKER_DELAY"] = f"{delay:g}"
    else:
        environ.pop("PAPAIA_FAKE_DOCKER_DELAY", None)
    if images:
        environ["PAPAIA_FAKE_DOCKER_IMAGES"] = str(images)
    else:
        environ.pop("PAPAIA_FAKE_DOCKER_IMAGES", None)


def _delay(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("--docker-delay must be a number of seconds") from None
    if not 0 <= value <= 600:  # noqa: PLR2004
        raise argparse.ArgumentTypeError("--docker-delay must be between 0 and 600")
    return value


def _count(raw: str) -> int:
    if not raw.isdigit() or not 1 <= int(raw) <= 20000:  # noqa: PLR2004
        raise argparse.ArgumentTypeError("--docker-images must be between 1 and 20000")
    return int(raw)


def _port(raw: str) -> int:
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:  # noqa: PLR2004
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return int(raw)


def _person(raw: str) -> str:
    if raw not in design_world.PERSONS:
        raise argparse.ArgumentTypeError(f"unknown person {raw!r}")
    return raw


def _state(raw: str) -> tuple[str, Path]:
    """`[PERSON=]FILE`: where the storage state of a test person goes (Anna without a name)."""
    person, sep, file = raw.partition("=")
    if not sep:
        person, file = "anna", raw
    if not file:
        raise argparse.ArgumentTypeError("--storage-state needs a file")
    return _person(person), Path(file)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=_port)
    parser.add_argument("--storage-state", type=_state, action="append", default=[])
    parser.add_argument("--list-pages", action="store_true", help="print the pages and stop")
    parser.add_argument("--list-persons", action="store_true", help="print the persons and stop")
    parser.add_argument("--person", type=_person, help="with --list-pages: only this person's")
    parser.add_argument("--mail-relay", action="store_true", help="a core with the mail relay on")
    parser.add_argument(
        "--with-backups", action="store_true", help="give the world restore points to list"
    )
    parser.add_argument(
        "--rag-catalog", action="store_true", help="make the connection store writable"
    )
    parser.add_argument(
        "--with-ingest", action="store_true", help="answer the ingest pages from a fake ingester"
    )
    parser.add_argument(
        "--docker-delay", type=_delay, default=0.0, help="seconds the stand-in docker waits"
    )
    parser.add_argument(
        "--docker-images", type=_count, default=0, help="images of a large synthetic set"
    )
    args = parser.parse_args(argv)
    if args.list_persons:
        print("\n".join(design_persons()))
        return 0
    if args.list_pages:
        print("\n".join(design_pages(person=args.person)))
        return 0
    if args.port is None or not args.storage_state:
        parser.error("--port and --storage-state are required")

    root = Path(tempfile.mkdtemp(prefix="papaia-manager-design-"))
    config, workspace = design_world.make_world(root, rag_catalog=args.rag_catalog)
    design_world.add_core_compose(workspace)
    if args.mail_relay:
        design_world.add_mail_relay(config, workspace)
    if args.with_backups:
        design_world.seed_backups(config, root)
    secret = secrets.token_urlsafe(32)
    host = f"http://127.0.0.1:{args.port}"
    os.environ.update(design_world.environment(config, workspace, host=host, secret=secret))
    # Before the application exists: nothing it starts may find the host's docker.
    use_fake_docker(
        os.environ, log=root / "docker.log", delay=args.docker_delay, images=args.docker_images
    )
    # Templates and static files are found relative to the application root.
    os.chdir(SRC)
    sys.path.insert(0, str(SRC))

    import uvicorn

    from app.main import create_app

    app = create_app()
    design_world.stand_ins(app)
    if args.with_ingest:
        # Imported here: its fakes import the application, which needs the settings above.
        import design_ingest

        design_ingest.seed(config)
        design_ingest.stand_ins(app, config)
    for person, state in args.storage_state:
        write_storage_state(state, port=args.port, secret=secret, person=person)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
