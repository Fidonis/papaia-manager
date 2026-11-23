"""Serve the manager with a stand-in RAG system for the browser path of tests/rag_browser.mjs.

Like tests/design_serve.py it binds 127.0.0.1 only, on an unprivileged port, with a session
secret made for this run, and writes a Playwright storage state that signs in a test
administrator. Qdrant, the ingester and LiteLLM are the in-memory fakes of the tests
(tests/fake_qdrant.py, tests/fake_ingest.py, tests/fake_litellm.py) behind
`httpx.MockTransport`, so the Embedding, Collections and Models & Usage pages have something
to show and to change; nothing talks to a real service. A test tool, never part of the image.

With --hostile the world carries the value design_world.HOSTILE, which is markup and script
at once, in the places the browser check of the enforced policy reads it from
(tests/csp_hostile_browser.mjs): the name the frame shows for the signed-in administrator, the
display name of an account in the simulated Keycloak, the name of a collection in the fake
Qdrant and the name of a notification recipient. The dashboard then also holds a group of tiles
whose icon is on another site (design_world.OFFSITE_ICON), written as a tiles.yaml of an older
release would be, past the editor's check.

Usage (from the repository root):
  uv run --project src python tests/rag_serve.py --port PORT --storage-state FILE [--hostile]
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
SRC = REPO / "src"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(REPO))

import design_serve  # noqa: E402
import design_world  # noqa: E402
from fake_keycloak import FakeKeycloak  # noqa: E402

MODEL = "nomic-embed-text"


def _port(raw: str) -> int:
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:  # noqa: PLR2004
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return int(raw)


def make_rag_world(root: Path) -> tuple[Path, Path]:
    """The configuration of design_world with the RAG secrets, documents and LiteLLM added."""
    from tests.fake_ingest import TOKEN
    from tests.fake_litellm import MASTER_KEY
    from tests.fake_qdrant import API_KEY

    config, workspace = design_world.make_world(root)
    from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH

    (config / "manager").mkdir(parents=True, exist_ok=True)
    (config / CONNECTIONS_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    docs = config / "ai" / "rag" / "documents"
    (docs / "handbook").mkdir(parents=True, exist_ok=True)
    (docs / "handbook" / "welcome.md").write_text("# Welcome\n", encoding="utf-8")
    (docs / "notes.txt").write_text("notes\n", encoding="utf-8")
    (config / "ai" / "rag" / ".env").write_text(
        f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET=s\nQI_API_TOKEN={TOKEN}\n",
        encoding="utf-8",
    )
    (config / "ai" / "litellm").mkdir(parents=True, exist_ok=True)
    (config / "ai" / "litellm" / ".env").write_text(
        f"LITELLM_MASTER_KEY={MASTER_KEY}\n", encoding="utf-8"
    )
    return config, workspace


def stand_ins(app: object, config: Path, *, hostile: bool = False) -> None:
    """Answer Qdrant, the ingester and LiteLLM from the fakes of the tests; with `hostile` the
    fake Qdrant also holds a collection named design_world.HOSTILE."""
    from fastapi import FastAPI

    from app.config import get_settings
    from app.core import runner
    from app.core.rag_collections import meta_point_id
    from app.routers.models_deps import get_litellm_transport
    from app.routers.rag_deps import get_http_transport
    from tests.fake_ingest import FakeIngest
    from tests.fake_litellm import FakeLitellm
    from tests.fake_qdrant import API_KEY, FakeQdrant, Fleet

    assert isinstance(app, FastAPI)
    qdrant = FakeQdrant(API_KEY)
    qdrant.add("kb", size=4, extra_points=12)
    qdrant.add("_collection_meta", size=1)
    qdrant.put(
        "_collection_meta",
        meta_point_id("kb"),
        {"collection": "kb", "embedding_model": MODEL, "vector_dimension": 4},
    )
    if hostile:
        qdrant.add(design_world.HOSTILE, size=4)
    fleet = Fleet()
    fleet.add("qdrant.test", qdrant)
    fleet.add("qdrant-ingest", FakeIngest(config))
    litellm = FakeLitellm()
    today = datetime.now(UTC).date()
    for back, model, tokens in ((0, "gpt-4o-mini", 3000), (1, "qwen-local", 1200)):
        day = today - timedelta(days=back)
        litellm.use(day, model, prompt=tokens // 3, completion=tokens - tokens // 3, ok=3)

    settings = get_settings().model_copy(
        update={
            "qdrant_url": "http://qdrant.test:6333",
            "qdrant_ingest_url": "http://qdrant-ingest:8300",
        }
    )

    async def idle(_kind: object = None) -> None:
        return None

    runner.find_runner = idle  # type: ignore[assignment]
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_http_transport] = lambda: fleet.transport()
    app.dependency_overrides[get_litellm_transport] = lambda: litellm.transport


def seed_hostile(config: Path, keycloak: FakeKeycloak) -> None:
    """An account whose display name and a notification recipient whose name is HOSTILE, and
    tiles whose icon is on another site."""
    from app.core.notifications import store
    from app.core.tiles import Tile, TileGroup, load_tiles, save_tiles

    tiles = load_tiles(str(config))
    tiles.groups.append(
        TileGroup(
            name="Elsewhere",
            tiles=[
                Tile(name="Wiki", href="https://wiki.example.test", icon=design_world.OFFSITE_ICON),
                Tile(
                    name="Keycloak", href="https://sso.example.test", icon=design_world.OFFSITE_ICON
                ),
            ],
        )
    )
    save_tiles(str(config), tiles)

    keycloak.add_user("hostile", email="hostile@example.test", first=design_world.HOSTILE)
    store.add_recipient(
        str(config),
        store.load_config(str(config)).revision,
        name=design_world.HOSTILE,
        kind="webhook",
        target="https://hooks.example.test/papaia",
        enabled=True,
        events=[],
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=_port, required=True)
    parser.add_argument("--storage-state", type=Path, required=True)
    parser.add_argument(
        "--hostile", action="store_true", help="put markup and script into names and texts"
    )
    args = parser.parse_args(argv)

    root = Path(tempfile.mkdtemp(prefix="papaia-manager-rag-"))
    config, workspace = design_world.make_world(root)
    secret = secrets.token_urlsafe(32)
    host = f"http://127.0.0.1:{args.port}"
    os.environ.update(design_world.environment(config, workspace, host=host, secret=secret))
    # Templates and static files are found relative to the application root; the fakes import
    # the application, so it is importable before the world gets its RAG parts.
    os.chdir(SRC)
    sys.path.insert(0, str(SRC))
    make_rag_world(root)

    import uvicorn

    from app.main import create_app

    app = create_app()
    keycloak = design_world.stand_ins(app)
    stand_ins(app, config, hostile=args.hostile)
    if args.hostile:
        seed_hostile(config, keycloak)
    design_serve.write_storage_state(
        args.storage_state,
        port=args.port,
        secret=secret,
        username=design_world.HOSTILE if args.hostile else None,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
