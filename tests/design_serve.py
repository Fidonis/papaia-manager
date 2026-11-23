"""Serve the manager in the stand-in world for the kit's browser checks (scripts/design-check.sh).

Binds 127.0.0.1 only, on an unprivileged port, with a session secret made for this run, and
writes a Playwright storage state that signs Anna in (tests/design_world.py). The manager
talks to no real service: Keycloak is the simulation of the tests, and pages that would ask
Docker, LiteLLM or Qdrant show their "unreachable" state. A test tool, never part of the image.

Usage (from the repository root):
  uv run --project src python tests/design_serve.py --port PORT --storage-state FILE
  uv run --project src python tests/design_serve.py --list-pages
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
SRC = REPO / "src"
sys.path.insert(0, str(TESTS))

import design_world  # noqa: E402

PAGES_FILE = REPO / "ui" / "pages.yaml"


def design_pages(pages_file: Path = PAGES_FILE) -> list[str]:
    """Every page of the list with a path, in its order, placeholders filled."""
    doc = yaml.safe_load(pages_file.read_text(encoding="utf-8"))
    return [design_world.sample_path(row["path"]) for row in doc["pages"] if "path" in row]


def write_storage_state(path: Path, *, port: int, secret: str) -> None:
    """A Playwright storage state with Anna's session cookie for http://127.0.0.1:<port>."""
    cookie = {
        "name": design_world.SESSION_COOKIE,
        "value": design_world.session_cookie(secret),
        "domain": "127.0.0.1",
        "path": "/",
        "expires": -1,
        # Plain http on the loopback: the cookie cannot be marked secure.
        "httpOnly": True,
        "secure": False,
        "sameSite": "Lax",
    }
    path.write_text(json.dumps({"cookies": [cookie], "origins": []}), encoding="utf-8")


def _port(raw: str) -> int:
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:  # noqa: PLR2004
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return int(raw)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=_port)
    parser.add_argument("--storage-state", type=Path)
    parser.add_argument("--list-pages", action="store_true", help="print the pages and stop")
    args = parser.parse_args(argv)
    if args.list_pages:
        print("\n".join(design_pages()))
        return 0
    if args.port is None or args.storage_state is None:
        parser.error("--port and --storage-state are required")

    root = Path(tempfile.mkdtemp(prefix="papaia-manager-design-"))
    config, workspace = design_world.make_world(root)
    secret = secrets.token_urlsafe(32)
    host = f"http://127.0.0.1:{args.port}"
    os.environ.update(design_world.environment(config, workspace, host=host, secret=secret))
    # Templates and static files are found relative to the application root.
    os.chdir(SRC)
    sys.path.insert(0, str(SRC))

    import uvicorn

    from app.main import create_app

    app = create_app()
    design_world.stand_ins(app)
    write_storage_state(args.storage_state, port=args.port, secret=secret)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
