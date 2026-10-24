"""A small read-only client for the LiteLLM proxy's HTTP API.

The Models page needs three answers: whether LiteLLM is up (`/health/readiness`), which models
it serves and at what price (`/model/info`), and what each model consumed per day
(`/user/daily/activity`). Nothing here can change LiteLLM: the client has no method that is not a
GET, and `READ_PATHS` is the whole list of routes it will send to. Teams, budgets, keys and the
model list stay in LiteLLM's own Admin UI.

LiteLLM authenticates with its master key, which the manager reads from the module's `.env` at
request time like the other secrets it needs (see `app.core.rag`). The key goes into one header
and nowhere else: it is not part of any message, log line or exception text, and neither is a
URL. As with `QdrantClient` and `IngestClient`, one client lives for one request.

A failure carries its message as a msgid with parameters (`Problem`) rather than as text, so a
reading that is cached for every account can be worded in each account's language.
"""
from __future__ import annotations

import re
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from app.core.envfile import load_env_file
from app.i18n import N_, gettext

DEFAULT_TIMEOUT = 15.0

# `/user/daily/activity` pages over the rows LiteLLM keeps (a day, a key, a model, an endpoint),
# not over days, so one day can arrive on several pages. A window of a year on a busy stack is a
# few thousand rows; the cap only stops a runaway.
PAGE_SIZE = 1000
MAX_PAGES = 50

# The routes the manager sends to, and the only ones. Anything else is a bug, caught in tests.
READ_PATHS = frozenset({"/health/readiness", "/model/info", "/user/daily/activity"})

_MODULE_ENV = Path("ai") / "litellm" / ".env"
_MASTER_KEY = "LITELLM_MASTER_KEY"

NO_KEY = N_(
    "LITELLM_MASTER_KEY is not set in ai/litellm/.env, so the manager cannot read from LiteLLM."
)
KEY_REFUSED = N_(
    "LiteLLM refused the master key. Check LITELLM_MASTER_KEY in ai/litellm/.env; LiteLLM reads "
    "it at start, so it needs a restart after a change."
)
NOT_REACHABLE = N_("LiteLLM is not reachable at %(where)s (%(error)s).")
REJECTED = N_("LiteLLM answered with an error: %(detail)s")
UNREADABLE = N_("LiteLLM sent an answer the manager could not read.")


@dataclass(frozen=True)
class Problem:
    """Why a reading is missing, as a msgid and its parameters. Never a secret, never a URL."""

    msgid: str
    params: Mapping[str, object] = field(default_factory=dict)

    def text(self) -> str:
        message = gettext(self.msgid)
        return message % dict(self.params) if self.params else message


class LitellmError(Exception):
    """LiteLLM could not be read."""

    def __init__(self, msgid: str, params: Mapping[str, object] | None = None) -> None:
        super().__init__(msgid)
        self.problem = Problem(msgid, dict(params or {}))

    def __str__(self) -> str:
        return self.problem.text()


class LitellmUnavailable(LitellmError):  # noqa: N818 - a state, not a failure of one request
    """LiteLLM cannot be used: not reachable, no key, or the key is refused."""


class LitellmRejected(LitellmError):  # noqa: N818 - an answer, not a failure of ours
    """LiteLLM answered, but not with something usable."""


@dataclass(frozen=True)
class LitellmSecrets:
    master_key: str = field(default="", repr=False)


def litellm_secrets(config_dir: str) -> LitellmSecrets:
    env = load_env_file(Path(config_dir) / _MODULE_ENV)
    return LitellmSecrets(master_key=env.get(_MASTER_KEY, "").strip())


_SAFE_URL = re.compile(r"^https?://[^\s\"'<>]+$")


def public_ui_url(config_dir: str) -> str:
    """The address of LiteLLM's own Admin UI in the browser, or "" when the stack names none.

    `LITELLM_PUBLIC_URL` is in the stack's root `.env` (the core derives it from the host name),
    the same value the dashboard's tiles resolve `{{LITELLM_PUBLIC_URL}}` from.
    """
    value = load_env_file(Path(config_dir) / ".env").get("LITELLM_PUBLIC_URL", "").strip()
    return value.rstrip("/") + "/ui" if _SAFE_URL.match(value) else ""


def _detail(response: httpx.Response) -> str:
    """LiteLLM's own message, or the bare status. Short: it is shown on a page."""
    fallback = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return fallback
    if not isinstance(body, dict):
        return fallback
    for key in ("error", "detail"):
        value = body.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("error")
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return fallback


class LitellmClient:
    def __init__(
        self,
        url: str,
        key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        verify: ssl.SSLContext | bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = url.rstrip("/")
        self._key = key
        self._client = httpx.AsyncClient(
            base_url=self._base, timeout=timeout, verify=verify, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> LitellmClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, authenticated: bool = True
    ) -> Any:
        """One GET, and the parsed body.

        Raises `LitellmUnavailable` for a transport failure, a missing key and a 401/403, and
        `LitellmRejected` for every other non-2xx answer or an answer that is not JSON.
        """
        if path not in READ_PATHS:
            raise ValueError(f"{path} is not a read route of this client")
        if authenticated and not self._key:
            raise LitellmUnavailable(NO_KEY)
        headers = {"Authorization": f"Bearer {self._key}"} if authenticated else {}
        try:
            response = await self._client.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            # The exception text can carry the full URL; the host is enough to act on.
            target = httpx.URL(self._base)
            where = f"{target.host}:{target.port}" if target.port else str(target.host)
            raise LitellmUnavailable(
                NOT_REACHABLE, {"where": where, "error": type(exc).__name__}
            ) from exc
        if response.status_code in (401, 403):
            raise LitellmUnavailable(KEY_REFUSED)
        if response.status_code >= 400:
            raise LitellmRejected(REJECTED, {"detail": _detail(response)})
        try:
            return response.json()
        except ValueError as exc:
            raise LitellmRejected(UNREADABLE) from exc

    # ── the calls the manager makes ─────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        """`GET /health/readiness`: free of the key, so it answers when the key is wrong.

        LiteLLM says only `status` and `db` here, no version.
        """
        body = await self._get("/health/readiness", authenticated=False)
        if not isinstance(body, dict):
            raise LitellmRejected(UNREADABLE)
        return body

    async def model_info(self) -> list[dict[str, Any]]:
        """`GET /model/info`: one entry per deployment, with its price and mode."""
        body = await self._get("/model/info")
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise LitellmRejected(UNREADABLE)
        return [entry for entry in data if isinstance(entry, dict)]

    async def daily_activity(self, start: date, end: date) -> tuple[list[dict[str, Any]], bool]:
        """`GET /user/daily/activity` for `start`..`end` (both included, UTC days).

        As the master key it covers the whole proxy. Returns the day entries of every page, in
        the order LiteLLM sent them (a day may repeat across pages: the caller adds them up), and
        whether every page was read.
        """
        results: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            body = await self._get(
                "/user/daily/activity",
                {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "include_current_utc_day": "true",
                    "page": page,
                    "page_size": PAGE_SIZE,
                },
            )
            chunk = body.get("results") if isinstance(body, dict) else None
            if not isinstance(chunk, list):
                raise LitellmRejected(UNREADABLE)
            results.extend(entry for entry in chunk if isinstance(entry, dict))
            metadata = body.get("metadata")
            if not (isinstance(metadata, dict) and metadata.get("has_more") is True):
                return results, True
        return results, False
