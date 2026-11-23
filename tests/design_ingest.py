"""An ingester for the stand-in world, for the browser path of the ingest lists.

design_serve.py --with-ingest puts it behind the manager: the fake ingester of the page tests
(tests/fake_ingest.py) and the fake Qdrant (tests/fake_qdrant.py) on the manager's HTTP
transport, a catalog with three jobs, a finished run and a leftover. The manager reads and
writes the catalog file as it does in an installation; nothing talks to a real service, and
the runner lookup, which would ask Docker, answers that no runner exists. Synthetic values only.
A test tool, never part of the image.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fake_ingest import TOKEN, FakeIngest
from fake_qdrant import API_KEY, FakeQdrant, Fleet
from fastapi import FastAPI

# The jobs of the catalog: one to run, one to delete, one that is switched off.
JOBS: tuple[dict[str, Any], ...] = tuple(
    {
        "id": job_id,
        "description": description,
        "source": {"type": "local", "label": job_id, "path": f"/data/local/{job_id}"},
        "target": {"collection": "kb", "connection": "default"},
        "mode": "upsert",
        "embedding": {"model": "nomic-embed-text"},
        **extra,
    }
    for job_id, description, extra in (
        ("handbook", "The staff handbook", {}),
        ("archive", "Old minutes", {}),
        ("drafts", "Unfinished texts", {"enabled": False}),
    )
)
LEFTOVER = {"job_id": "retired", "collection": "kb", "state_rows": 2, "points": 1200}


def seed(config: Path) -> None:
    """The files of the RAG system the ingest pages read: secrets, connections, catalog."""
    rag = config / "ai" / "rag"
    (rag / "catalog").mkdir(parents=True, exist_ok=True)
    (config / "manager").mkdir(parents=True, exist_ok=True)
    (rag / ".env").write_text(
        f"QI_CONNECTIONS_SECRET=design\nQI_API_TOKEN={TOKEN}\nQI_TIMEZONE=Europe/Berlin\n"
        f"QDRANT_JWT_SECRET={API_KEY}\n",
        encoding="utf-8",
    )
    (rag / "catalog" / "connections.yaml").write_text(
        yaml.safe_dump(
            {"version": 1, "connections": [{"name": "default", "url": "http://qdrant:6333"}]}
        ),
        encoding="utf-8",
    )
    (rag / "catalog" / "jobs.yaml").write_text(
        yaml.safe_dump({"version": 1, "jobs": [dict(job) for job in JOBS]}, sort_keys=False),
        encoding="utf-8",
    )


def stand_ins(app: FastAPI, config: Path) -> FakeIngest:
    """Answer the ingest pages from the fake ingester; a finished run and a leftover to list."""
    from app.core import runner
    from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH
    from app.routers.rag_deps import get_http_transport

    assert (config / CONNECTIONS_RELPATH).is_file(), "seed() writes the connections first"
    ingest = FakeIngest(config)
    ingest.load_catalog()
    finished = ingest.seed_run("handbook", "2026-10-04T09:00:00+00:00", log_lines=3)
    ingest.finish(finished, files_seen=12, docs_indexed=10, docs_unchanged=2)
    ingest.orphans = [dict(LEFTOVER)]
    qdrant = FakeQdrant(API_KEY)
    qdrant.add("kb", size=4)
    fleet = Fleet()
    fleet.add("qdrant-ingest", ingest)
    fleet.add("qdrant", qdrant)
    app.dependency_overrides[get_http_transport] = fleet.transport

    async def no_runner(_kind: object = None) -> None:
        return None

    runner.find_runner = no_runner  # type: ignore[assignment]
    return ingest
