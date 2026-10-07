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

For the job management pages it also serves the rest of the control plane: the job list, the
documents of a job, the leftovers, a validation of catalog text and `/health` with the
`features` an ingester announces. `features` is settable, and an empty list is an ingester
without any of them: the routes of those features then answer 404 and the run rows carry no
progress fields, exactly like the 0.3.0 the core still pins.

`delete_runs` is the one that deletes: `DELETE /v1/jobs/{id}/runs` takes `since` (included) and
`until` (excluded), counts with `dry_run`, refuses without `confirm=true`, and never deletes a
run that is still working. It answers for jobs that are not in the catalog, because the history
of a deleted job is what it is for.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import yaml

TOKEN = "test-ingest-token"

_RUN_FIELDS = {"mode", "full_scope", "dry_run", "skip_sync", "force", "queue", "delete_vanished"}
ALL_FEATURES = ["run_progress", "documents", "validate", "delete_runs", "secret_store"]
_SECRET_KEYS = {
    "pass",
    "access_key_id",
    "secret_access_key",
    "key_file",
    "service_account_json",
    "token",
    "key",
    "sas_url",
}
_DEFAULT_SECTIONS = ("embedding", "chunking", "filters", "schedule", "safety")
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
        self.features: list[str] = list(ALL_FEATURES)
        self.version = "1.0.0"
        self.deps = {"qdrant": True, "embeddings": True, "tika": True}
        # What the jobs of the catalog have embedded, by job id (the `documents` rows).
        self.documents: dict[str, list[dict[str, Any]]] = {}
        # Leftovers: state the ingester has for jobs that are not in its catalog.
        self.orphans: list[dict[str, Any]] = []
        # What `/v1/config/validate` answers instead of checking the text, when set.
        self.validation_errors: list[dict[str, Any]] | None = None
        self.validated: list[str] = []
        self.paused: set[str] = set()
        self.preview_files: dict[str, list[dict[str, Any]]] = {}
        self.error_status: int | None = None
        # The queries of the run-deletion calls, in order.
        self.delete_runs_calls: list[dict[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- seeding and inspection ------------------------------------------------

    def load_catalog(self) -> None:
        """What a reload does, without the HTTP: read the file and serve it."""
        if not self.jobs_file.exists():
            self.jobs = {}
            return
        document = yaml.safe_load(self.jobs_file.read_text(encoding="utf-8")) or {}
        defaults = document.get("defaults") or {}
        loaded: dict[str, dict[str, Any]] = {}
        for job in document.get("jobs") or []:
            merged = json.loads(json.dumps(job))
            for section in _DEFAULT_SECTIONS:
                inherited = defaults.get(section) or {}
                merged[section] = {**inherited, **(merged.get(section) or {})}
            merged.setdefault("enabled", True)
            merged.setdefault("mode", "upsert")
            source = merged.get("source") or {}
            for key in _SECRET_KEYS & set(source):
                source[key] = "***"  # the ingester never reports a secret
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
            "sync_status": None,
            "sync_stderr_tail": None,
            **dict.fromkeys(_COUNTERS, 0),
        }
        if "run_progress" in self.features:
            self.runs[run_id].update(
                {"dry_run": False, "files_done": 0, "phase": "syncing", "current": None}
            )
        self.events[run_id] = []
        return run_id

    def seed_run(
        self,
        job_id: str,
        started_at: str,
        status: str = "success",
        *,
        log_lines: int = 1,
    ) -> str:
        """A run of any age that is there already, with a few lines of log."""
        run_id = self.start_run(job_id)
        run = self.runs[run_id]
        run["started_at"] = started_at
        if status != "running":
            run["status"] = status
            run["finished_at"] = started_at
            if "run_progress" in self.features:
                run.update({"phase": None, "current": None})
        self.events[run_id] = [
            {"seq": n, "level": "info", "message": f"line {n}"} for n in range(1, log_lines + 1)
        ]
        return run_id

    def progress(self, run_id: str, **fields: Any) -> None:
        """What a run reports while it works (only meaningful with `run_progress`)."""
        self.runs[run_id].update(fields)

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
        if "run_progress" in self.features:
            run.update({"phase": None, "current": None})
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
            return _json(
                200,
                {
                    "status": "ok",
                    "version": self.version,
                    "jobs_loaded": len(self.jobs),
                    "config_error": None,
                    "deps": self.deps,
                    "features": self.features,
                },
            )
        if self.error_status is not None:
            return _detail(self.error_status, "the ingester is unwell")
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return _detail(401, "invalid token")
        body = json.loads(request.content) if request.content else None
        query = {key: values[-1] for key, values in parse_qs(request.url.query.decode()).items()}
        method = request.method

        if path == "/v1/config" and method == "GET":
            return self._config()
        if path == "/v1/config/reload" and method == "POST":
            return self._reload()
        if path == "/v1/config/validate" and method == "POST":
            return self._validate(body)
        parts = path.split("/")[2:]
        if parts == ["jobs"] and method == "GET":
            return _json(200, [self._summary(job) for job in self.jobs.values()])
        if parts[:1] == ["jobs"] and len(parts) == 2 and method == "GET":
            job = self.jobs.get(parts[1])
            if job is None:
                return _detail(404, f"unknown job '{parts[1]}'")
            return _json(
                200,
                {
                    "config": job,
                    "paused": parts[1] in self.paused,
                    "next_run_at": None,
                    "runs": [],
                },
            )
        if parts[:1] == ["jobs"] and len(parts) == 3 and method == "GET":
            return self._job_sub(parts[1], parts[2], query)
        if parts[:1] == ["jobs"] and parts[2:] == ["runs"] and method == "DELETE":
            return self._delete_runs(parts[1], query)
        if parts[:1] == ["jobs"] and len(parts) == 3 and parts[2] == "resume" and method == "POST":
            self.paused.discard(parts[1])
            return _json(200, {"enabled": True})
        if parts == ["orphans"] and method == "GET":
            return _json(200, self.orphans)
        if parts[:1] == ["orphans"] and len(parts) == 2 and method == "DELETE":
            return self._delete_orphan(parts[1], query)
        if parts[:1] == ["jobs"] and len(parts) == 3 and parts[2] == "run" and method == "POST":
            return self._run(parts[1], body)
        if parts == ["runs"] and method == "GET":
            return self._list_runs(query)
        if parts[:1] == ["runs"] and len(parts) == 2:
            return self._one_run(parts[1], method)
        return _detail(404, "Not Found")

    def _summary(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["id"])
        runs = [r for r in self.runs.values() if r["job_id"] == job_id]
        runs.sort(key=lambda run: run["started_at"], reverse=True)
        schedule = job.get("schedule") or {}
        rows = self.documents.get(job_id, [])
        return {
            "id": job_id,
            "enabled": job.get("enabled", True),
            "paused": job_id in self.paused,
            "source": {
                "type": (job.get("source") or {}).get("type"),
                "label": (job.get("source") or {}).get("label"),
            },
            "collection": (job.get("target") or {}).get("collection"),
            "connection": (job.get("target") or {}).get("connection"),
            "mode": job.get("mode"),
            "cron": schedule.get("cron"),
            "every": schedule.get("every"),
            "next_run_at": None,
            "last_run": runs[0] if runs else None,
            "documents": {
                "total": len(rows),
                "chunks": sum(int(row.get("chunk_count", 0)) for row in rows),
            },
        }

    def _job_sub(self, job_id: str, what: str, query: dict[str, str]) -> httpx.Response:
        if job_id not in self.jobs:
            return _detail(404, f"unknown job '{job_id}'")
        if what == "preview":
            files = self.preview_files.get(job_id, [])
            return _json(200, {"files": files, "count": len(files)})
        if what == "documents" and "documents" in self.features:
            rows = list(self.documents.get(job_id, []))
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
            status = query.get("status")
            if status:
                rows = [row for row in rows if row["status"] == status]
            needle = query.get("q")
            if needle:
                rows = [row for row in rows if needle.lower() in row["rel_path"].lower()]
            if query.get("run_id"):
                rows = [row for row in rows if row.get("last_run_id") == query["run_id"]]
            offset, limit = int(query.get("offset", 0)), int(query.get("limit", 50))
            return _json(
                200, {"total": len(rows), "counts": counts, "items": rows[offset : offset + limit]}
            )
        return _detail(404, "Not Found")

    def _validate(self, body: Any) -> httpx.Response:
        if "validate" not in self.features:
            return _detail(404, "Not Found")
        raw = (body or {}).get("raw")
        self.validated.append(str(raw))
        if self.validation_errors is not None:
            errors = self.validation_errors
        else:
            try:
                yaml.safe_load(raw)
                errors = []
            except yaml.YAMLError as exc:
                errors = [{"job_id": None, "field": "jobs_file", "message": f"invalid YAML: {exc}"}]
        return _json(200, {"ok": not errors, "errors": errors, "jobs": 0})

    def _delete_orphan(self, job_id: str, query: dict[str, str]) -> httpx.Response:
        if query.get("confirm") != "true":
            return _detail(400, "pass ?confirm=true to delete")
        if job_id in self.jobs:
            return _detail(409, f"job '{job_id}' is still in the catalog")
        match = next((o for o in self.orphans if o["job_id"] == job_id), None)
        self.orphans = [o for o in self.orphans if o["job_id"] != job_id]
        return _json(
            200,
            {
                "deleted_points": match["points"] if match else 0,
                "deleted_rows": match["state_rows"] if match else 0,
            },
        )

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
        if "run_progress" in self.features:
            self.runs[run_id]["dry_run"] = bool(request.get("dry_run"))
        return _json(202, {"run_id": run_id, "queued": False})

    def _delete_runs(self, job_id: str, query: dict[str, str]) -> httpx.Response:
        if "delete_runs" not in self.features:
            return _detail(404, "Not Found")  # an ingester that predates the route
        self.delete_runs_calls.append({"job_id": job_id, **query})
        dry_run = query.get("dry_run") == "true"
        if query.get("confirm") != "true" and not dry_run:
            return _detail(400, "pass ?confirm=true to delete, or ?dry_run=true to count")

        def instant(key: str) -> datetime | None:
            value = query.get(key)
            if not value:
                return None
            moment = datetime.fromisoformat(value)
            return moment if moment.tzinfo else moment.replace(tzinfo=UTC)

        since, until = instant("since"), instant("until")
        if since and until and since > until:
            return _detail(422, "since is later than until")
        in_range = [
            run
            for run in self.runs.values()
            if run["job_id"] == job_id
            and (since is None or datetime.fromisoformat(run["started_at"]) >= since)
            and (until is None or datetime.fromisoformat(run["started_at"]) < until)
        ]
        deletable = [run for run in in_range if run["status"] != "running"]
        events = sum(len(self.events.get(run["run_id"], [])) for run in deletable)
        if not dry_run:
            for run in deletable:
                del self.runs[run["run_id"]]
                self.events.pop(run["run_id"], None)
        return _json(
            200,
            {
                "matched": len(deletable),
                "matched_events": events,
                "deleted_runs": 0 if dry_run else len(deletable),
                "deleted_events": 0 if dry_run else events,
                "skipped_running": len(in_range) - len(deletable),
                "dry_run": dry_run,
            },
        )

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
