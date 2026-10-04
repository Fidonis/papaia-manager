"""A small async client for Qdrant's REST API.

The Collections page needs a handful of calls (list, create and delete a collection,
read and write points), so this is `httpx` with Qdrant's error shape rather than the
official client and its gRPC and numpy dependencies.

Two kinds of failure are told apart, because the page treats them differently:

* `QdrantUnavailable`: Qdrant cannot be used at all, either because it cannot be
  reached or because it refuses the api-key. The page shows the reason instead of a
  list.
* `QdrantError`: Qdrant answered and said no to this one request (a missing collection,
  a name that already exists). The caller decides what that means.

The api-key goes into one header and nowhere else: it is not part of any message,
log line or exception text.
"""
from __future__ import annotations

from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_TIMEOUT = 10.0


class QdrantError(Exception):
    """Qdrant answered with an error status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class QdrantUnavailable(QdrantError):  # noqa: N818 - a state, not a failure of one request
    """Qdrant cannot be used: not reachable, or the api-key is refused."""


def collection_path(name: str) -> str:
    """The URL path of a collection; the name is quoted, whatever it contains."""
    return f"/collections/{quote(name, safe='')}"


def _error_detail(response: httpx.Response) -> str:
    """Qdrant's own message, `{"status": {"error": "..."}}`, or the bare status."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    status = body.get("status") if isinstance(body, dict) else None
    if isinstance(status, dict) and isinstance(status.get("error"), str):
        return str(status["error"])
    return f"HTTP {response.status_code}"


class QdrantClient:
    """One client per request: the page opens it, uses it and closes it."""

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        verify: bool | str = True,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = url.rstrip("/")
        headers = {"api-key": api_key} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers=headers,
            timeout=timeout,
            verify=verify,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> QdrantClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Send one request and return Qdrant's `result`.

        Raises `QdrantUnavailable` for a transport failure and for 401/403, and
        `QdrantError` for every other non-2xx answer.
        """
        try:
            response = await self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            # The exception text can carry the full URL; the host is enough to act on.
            target = httpx.URL(self._base)
            where = f"{target.host}:{target.port}" if target.port else str(target.host)
            raise QdrantUnavailable(
                0, f"Qdrant is not reachable at {where} ({type(exc).__name__})"
            ) from exc

        if response.status_code in (401, 403):
            raise QdrantUnavailable(
                response.status_code,
                "Qdrant refused the api-key. Check QDRANT_JWT_SECRET in ai/rag/.env.",
            )
        if response.status_code >= 400:
            raise QdrantError(response.status_code, _error_detail(response))

        try:
            body = response.json()
        except ValueError as exc:
            raise QdrantError(response.status_code, "Qdrant sent an unreadable answer") from exc
        return body.get("result") if isinstance(body, dict) else None
