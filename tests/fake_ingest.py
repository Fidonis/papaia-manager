"""An ingester that speaks just enough of its REST control plane, for the Embedding tests.

An in-memory fake behind `httpx.MockTransport`, in the ingester's own error shapes. Unlike a
canned answer it reads the real `jobs.yaml` on a reload, so a test that checks what the manager
wrote is checking the file the ingester would have read. A run stays `running` until the test
finishes it with `finish`, which is how a test stands in for the time an embedding takes.

It reproduces the behaviours the manager depends on:

* every `/v1` route needs the bearer token (401 otherwise);
* a run request with a field the ingester does not know is a 422 (`extra_forbidden`), which is
  how an ingester without `delete_vanished` answers, and `delete_vanished: false` is refused
  for any mode but `upsert`;
* an unknown job is a 404 *after* the body validated, so a probe with an id no job can have
  tells the two ingesters apart without running anything;
* a catalog with an invalid job is not applied and the previous one keeps serving.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import yaml

TOKEN = "test-ingest-token"

_RUN_FIELDS = {"mode", "full_scope", "dry_run", "skip_sync", "force", "queue", "delete_vanished"}
_COUNTERS = (
    "files_seen",
    "docs_indexed",
    "docs_unchanged",
    "docs_skipped_changed",
    "docs_failed",
    "docs_deleted",
    "chunks_upserted",
    "bytes_read",
)


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def _detail(status: int, detail: Any) -> httpx.Response:
    return _json(status, {"detail": detail})


class FakeIngest:
    def __init__(self, config_dir: Path, token: str = TOKEN) -> None:
        self.token = token
        self.jobs_file = Path(config_dir) / "ai" / "rag" / "catalog" / "jobs.yaml"
        self.supports_delete_vanished = True
        self.down = False
        # Errors the next reloads report instead of applying the catalog.
        self.catalog_errors: list[dict[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.run_bodies: list[tuple[str, dict[str, Any]]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.applied = True

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- seeding and inspection ------------------------------------------------

    def load_catalog(self) -> None:
        """What a reload does, without the HTTP: read the file and serve it."""
        document = yaml.safe_load(self.jobs_file.read_text(encoding="utf-8")) or {}
        defaults = document.get("defaults") or {}
        loaded: dict[str, dict[str, Any]] = {}
        for job in document.get("jobs") or []:
            merged = json.loads(json.dumps(job))
            embedding = {**(defaults.get("embedding") or {}), **(merged.get("embedding") or {})}
            merged["embedding"] = embedding
            filters = merged.get("filters") or {}
            filters.setdefault("include", [])
            filters.setdefault("exclude", [])
            merged["filters"] = filters
            merged.setdefault("mode", "upsert")
            loaded[str(merged["id"])] = merged
        self.jobs = loaded

    def start_run(self, job_id: str, mode: str = "upsert") -> str:
        """A run that is working, as if something else had started it."""
        run_id = str(uuid.uuid4())
        self.runs[run_id] = {
            "run_id": run_id,
            "job_id": job_id,
            "mode": mode,
            "full_scope": None,
            "trigger": "manual_rest",
            "started_at": f"2026-10-05T10:00:{len(self.runs):02d}+00:00",
            "finished_at": None,
            "status": "running",
            "error": None,
            **dict.fromkeys(_COUNTERS, 0),
        }
        self.events[run_id] = []
        return run_id

    def finish(
        self,
        run_id: str,
        status: str = "success",
        *,
        error: str | None = None,
        events: list[dict[str, Any]] | None = None,
        **counters: int,
    ) -> None:
        run = self.runs[run_id]
        run["status"] = status
        run["finished_at"] = "2026-10-05T10:05:00+00:00"
        run["error"] = error
        run.update(counters)
        self.events[run_id].extend(events or [])

    def last_run_id(self) -> str:
        return next(reversed(self.runs))

    # -- the wire ---------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/health":
            return _json(200, {"status": "ok"})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return _detail(401, "invalid token")
        body = json.loads(request.content) if request.content else None
        query = {key: values[-1] for key, values in parse_qs(request.url.query.decode()).items()}
        method = request.method

        if path == "/v1/config" and method == "GET":
            return self._config()
        if path == "/v1/config/reload" and method == "POST":
            return self._reload()
        parts = path.split("/")[2:]
        if parts[:1] == ["jobs"] and len(parts) == 2 and method == "GET":
            job = self.jobs.get(parts[1])
            if job is None:
                return _detail(404, f"unknown job '{parts[1]}'")
            return _json(200, {"config": job, "paused": False, "next_run_at": None, "runs": []})
        if parts[:1] == ["jobs"] and len(parts) == 3 and parts[2] == "run" and method == "POST":
            return self._run(parts[1], body)
        if parts == ["runs"] and method == "GET":
            return self._list_runs(query)
        if parts[:1] == ["runs"] and len(parts) == 2:
            return self._one_run(parts[1], method)
        return _detail(404, "Not Found")

    def _config(self) -> httpx.Response:
        return _json(
            200,
            {
                "path": "/config/catalog/jobs.yaml",
                "valid": not self.catalog_errors,
                "applied": self.applied,
                "errors": self.catalog_errors,
            },
        )

    def _reload(self) -> httpx.Response:
        if self.catalog_errors:
            # The previous registry keeps serving.
            self.applied = False
        else:
            self.load_catalog()
            self.applied = True
        return self._config()

    def _run(self, job_id: str, body: Any) -> httpx.Response:
        request = body or {}
        unknown = sorted(set(request) - _RUN_FIELDS)
        if "delete_vanished" in request and not self.supports_delete_vanished:
            unknown.append("delete_vanished")
        if unknown:
            return _detail(
                422,
                [
                    {
                        "type": "extra_forbidden",
                        "loc": ["body", name],
                        "msg": "Extra inputs are not permitted",
                    }
                    for name in unknown
                ],
            )
        job = self.jobs.get(job_id)
        if job is None:
            return _detail(404, f"unknown job '{job_id}'")
        mode = request.get("mode") or job.get("mode", "upsert")
        if request.get("delete_vanished") is False and mode != "upsert":
            return _detail(422, "delete_vanished: false is only valid for mode 'upsert'")
        active = next(
            (r for r in self.runs.values() if r["job_id"] == job_id and r["status"] == "running"),
            None,
        )
        if active is not None:
            return _detail(409, {"error": "already_running", "run_id": active["run_id"]})
        self.run_bodies.append((job_id, request))
        run_id = self.start_run(job_id, mode)
        self.runs[run_id]["full_scope"] = request.get("full_scope")
        return _json(202, {"run_id": run_id, "queued": False})

    def _list_runs(self, query: dict[str, str]) -> httpx.Response:
        rows = [
            run
            for run in self.runs.values()
            if query.get("job_id") in (None, run["job_id"])
            and query.get("status") in (None, run["status"])
        ]
        rows.sort(key=lambda run: run["started_at"], reverse=True)
        return _json(200, rows[: int(query.get("limit", 50))])

    def _one_run(self, run_id: str, method: str) -> httpx.Response:
        run = self.runs.get(run_id)
        if run is None:
            return _detail(404, f"unknown run '{run_id}'")
        if method == "GET":
            return _json(200, {"run": run, "events": self.events.get(run_id, [])})
        if method == "DELETE":
            if run["status"] != "running":
                return _detail(409, f"run '{run_id}' is not running")
            run["status"] = "interrupted"
            run["finished_at"] = "2026-10-05T10:06:00+00:00"
            run["error"] = "interrupted by shutdown"
            return _json(202, {"aborting": run_id})
        return _detail(404, "Not Found")
