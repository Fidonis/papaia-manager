"""A small async client for the ingester's REST control plane (`/v1`).

The ingester authenticates REST with one static bearer token (`QI_API_TOKEN`), not with a
Keycloak token, so the manager reads that token from the RAG module's `.env` like the other
secrets it needs. The control plane can start, inspect and abort runs of jobs that exist in
`jobs.yaml`; it cannot create a job or take a file, which is why `app.core.ingest.catalog`
writes the catalog and `app.core.ingest.uploads` stages the files.

The token goes into one header and nowhere else: it is not part of any message, log line or
exception text. As with `QdrantClient`, one client lives for one request.
"""
from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from app.core.ingest.errors import IngestRejected, IngestTooOld, IngestUnavailable

DEFAULT_TIMEOUT = 15.0

NO_TOKEN = (
    "QI_API_TOKEN is not set in ai/rag/.env, so the manager cannot call the ingester."
)
TOKEN_REFUSED = (
    "The ingester refused the API token. Check QI_API_TOKEN in ai/rag/.env; the ingester "
    "reads it at start, so it needs a restart after a change."
)

# An id the ingester's job pattern forbids (`^[a-z0-9]...`), so no job can ever have it.
# A run request for it is answered 404 by an ingester that knows the run option and 422 by
# one that does not, which makes it a probe with no side effect.
_PROBE_JOB = "_probe"


def _detail(response: httpx.Response) -> tuple[str, Any]:
    """The ingester's own message and the parsed body, or the bare status."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}", None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail, body
    if isinstance(detail, dict):
        text = detail.get("error") or detail.get("message")
        return (str(text) if text else f"HTTP {response.status_code}"), body
    if isinstance(detail, list):
        parts = [
            str(item.get("msg")) for item in detail if isinstance(item, dict) and item.get("msg")
        ]
        if parts:
            return "; ".join(parts), body
    return f"HTTP {response.status_code}", body


class IngestClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = url.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> IngestClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        if not self._token:
            raise IngestUnavailable(NO_TOKEN)
        try:
            response = await self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            # The exception text can carry the full URL; the host is enough to act on.
            target = httpx.URL(self._base)
            where = f"{target.host}:{target.port}" if target.port else str(target.host)
            raise IngestUnavailable(
                f"The ingester is not reachable at {where} ({type(exc).__name__})."
            ) from exc
        if response.status_code == 401:
            raise IngestUnavailable(TOKEN_REFUSED)
        return response

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send one request and return the parsed body.

        Raises `IngestUnavailable` for a transport failure, a missing token and a 401, and
        `IngestRejected` for every other non-2xx answer.
        """
        response = await self._send(method, path, json=json, params=params)
        if response.status_code >= 400:
            detail, body = _detail(response)
            raise IngestRejected(response.status_code, detail, body)
        try:
            return response.json()
        except ValueError as exc:
            raise IngestRejected(
                response.status_code, "The ingester sent an unreadable answer"
            ) from exc

    # ── the calls the manager makes ─────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        """`GET /health`: free of the token, so it answers even when the token is wrong.

        That is the point of asking it first: the page can say "the ingester is up, but it
        refuses the token" instead of "the ingester is unavailable".
        """
        try:
            response = await self._client.get("/health")
        except httpx.HTTPError as exc:
            target = httpx.URL(self._base)
            where = f"{target.host}:{target.port}" if target.port else str(target.host)
            raise IngestUnavailable(
                f"The ingester is not reachable at {where} ({type(exc).__name__})."
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400 or not isinstance(body, dict):
            raise IngestRejected(response.status_code, "The ingester's health check failed")
        return body

    async def config(self) -> dict[str, Any]:
        """The catalog state: `valid`, `applied` and the errors per job."""
        result = await self.request("GET", "/v1/config")
        return result if isinstance(result, dict) else {}

    async def reload(self) -> dict[str, Any]:
        """Read `jobs.yaml` again now, instead of at the next poll."""
        result = await self.request("POST", "/v1/config/reload")
        return result if isinstance(result, dict) else {}

    async def job(self, job_id: str) -> dict[str, Any] | None:
        try:
            result = await self.request("GET", f"/v1/jobs/{job_id}")
        except IngestRejected as exc:
            if exc.status == 404:
                return None
            raise
        return result if isinstance(result, dict) else None

    async def list_jobs(self) -> list[dict[str, Any]]:
        """`GET /v1/jobs`: the jobs the ingester has loaded, with their last run and totals."""
        result = await self.request("GET", "/v1/jobs")
        return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []

    async def documents(self, job_id: str, **params: Any) -> dict[str, Any] | None:
        """`GET /v1/jobs/{id}/documents`, or None for an ingester that does not have it."""
        try:
            result = await self.request(
                "GET",
                f"/v1/jobs/{job_id}/documents",
                params={key: value for key, value in params.items() if value is not None},
            )
        except IngestRejected as exc:
            if exc.status == 404:
                return None
            raise
        return result if isinstance(result, dict) else None

    async def validate(self, raw: str) -> dict[str, Any] | None:
        """`POST /v1/config/validate`: would this catalog text load? None if not supported."""
        try:
            result = await self.request("POST", "/v1/config/validate", json={"raw": raw})
        except IngestRejected as exc:
            if exc.status in (404, 405):
                return None
            raise
        return result if isinstance(result, dict) else None

    async def preview(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """`GET /v1/jobs/{id}/preview`: what a run would pick up, without running."""
        result = await self.request("GET", f"/v1/jobs/{job_id}/preview", params={"limit": limit})
        files = result.get("files") if isinstance(result, dict) else None
        return [row for row in files if isinstance(row, dict)] if isinstance(files, list) else []

    async def orphans(self) -> list[dict[str, Any]]:
        result = await self.request("GET", "/v1/orphans")
        return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []

    async def delete_orphan(self, job_id: str) -> dict[str, Any]:
        result = await self.request(
            "DELETE", f"/v1/orphans/{job_id}", params={"confirm": "true"}
        )
        return result if isinstance(result, dict) else {}

    async def delete_runs(
        self,
        job_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any] | None:
        """`DELETE /v1/jobs/{id}/runs`: delete the run history of a job, or only count it.

        `since` is inclusive and `until` exclusive, both ISO instants. `None` if the ingester
        has no such route (404 or 405): an older release, which keeps the history.
        """
        params: dict[str, Any] = {"dry_run": "true"} if dry_run else {"confirm": "true"}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        try:
            result = await self.request("DELETE", f"/v1/jobs/{job_id}/runs", params=params)
        except IngestRejected as exc:
            if exc.status in (404, 405):
                return None
            raise
        return result if isinstance(result, dict) else {}

    async def resume(self, job_id: str) -> None:
        """Lift the ingester's own in-memory pause of a job (the manager pauses with `enabled`)."""
        await self.request("POST", f"/v1/jobs/{job_id}/resume")

    async def run(self, job_id: str, body: dict[str, Any]) -> str:
        """Start a run and return its id. `IngestRejected` carries a 409 for a busy job."""
        try:
            result = await self.request("POST", f"/v1/jobs/{job_id}/run", json=body)
        except IngestRejected as exc:
            if exc.status == 422 and "delete_vanished" in _text_of(exc):
                raise IngestTooOld(
                    "This ingester does not support updating without deleting. "
                    "Use an ingester release that has the delete_vanished run option."
                ) from exc
            raise
        run_id = result.get("run_id") if isinstance(result, dict) else None
        if not isinstance(run_id, str) or not run_id:
            raise IngestRejected(202, "The ingester did not name the run it started")
        return run_id

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """`{run, events}`, or None for a run the ingester no longer knows."""
        try:
            result = await self.request("GET", f"/v1/runs/{run_id}")
        except IngestRejected as exc:
            if exc.status == 404:
                return None
            raise
        return result if isinstance(result, dict) else None

    async def list_runs(
        self, *, job_id: str | None = None, status: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if job_id is not None:
            params["job_id"] = job_id
        if status is not None:
            params["status"] = status
        result = await self.request("GET", "/v1/runs", params=params)
        return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []

    async def abort(self, run_id: str) -> None:
        await self.request("DELETE", f"/v1/runs/{run_id}")

    async def supports_update_without_delete(self) -> bool:
        """Whether this ingester knows `delete_vanished`, found out without starting anything."""
        response = await self._send(
            "POST", f"/v1/jobs/{_PROBE_JOB}/run", json={"delete_vanished": False}
        )
        if response.status_code == 404:
            return True
        if response.status_code == 422:
            detail, body = _detail(response)
            return "delete_vanished" not in f"{detail} {body}"
        detail, _ = _detail(response)
        raise IngestRejected(response.status_code, detail)


def _text_of(exc: IngestRejected) -> str:
    return f"{exc.detail} {exc.body}"
