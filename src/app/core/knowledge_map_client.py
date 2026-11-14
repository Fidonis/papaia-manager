"""The admin API of the map server (`knowledge-map-mcp`), seen from the manager.

The map server answers what is not on disk: routing a question as a role (without recording
it), its usage aggregates, gold-set runs and their history, and the refinement suggestions it
mines from use. The bearer is the static `KM_ADMIN_TOKEN` from the module's `.env`, read at
request time; the server has no admin API without one, which is a state the page explains, not
an error.

The manager's administrators are the server's only admin callers: the manager checks the role
and writes the audit entry, the server trusts the token. Nothing a request says is joined to
a URL except a suggestion id, which is checked against its fixed shape first.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

import httpx

from app.i18n import gettext as _

TIMEOUT_SECONDS = 20.0
# Gold-set runs embed every question; give them longer.
EVAL_TIMEOUT_SECONDS = 120.0
_SUGGESTION_ID = re.compile(r"^[a-f0-9]{8,64}$")


class MapServerError(Exception):
    """The map server could not be asked, or refused. The message is shown as it is."""


class MapServerClient:
    def __init__(
        self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._transport = transport

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        seconds: float = TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        if not self._token:
            raise MapServerError(
                _("The knowledge map has no admin token; run setup to generate KM_ADMIN_TOKEN.")
            )
        try:
            async with httpx.AsyncClient(
                base_url=self._base, transport=self._transport, timeout=seconds
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.HTTPError as exc:
            raise MapServerError(
                _("The knowledge map server can't be reached (%(value)s).")
                % {"value": type(exc).__name__}
            ) from exc
        if response.status_code == 404 and path.startswith("/admin/api/map"):
            raise MapServerError(_("The knowledge map server runs without an admin API."))
        if response.status_code in (401, 403):
            raise MapServerError(_("The knowledge map server refused the admin token."))
        if response.status_code >= 400:
            detail = ""
            with contextlib.suppress(ValueError, AttributeError):
                detail = str(response.json().get("error") or "")
            raise MapServerError(
                _("The knowledge map server answered %(code)s %(detail)s")
                % {"code": response.status_code, "detail": detail}
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise MapServerError(_("The knowledge map server sent no JSON.")) from exc
        if not isinstance(data, dict):
            raise MapServerError(_("The knowledge map server sent no JSON."))
        return data

    async def map(self) -> dict[str, Any]:
        return await self._call("GET", "/admin/api/map")

    async def simulate(self, need: str, roles: list[str] | None) -> dict[str, Any]:
        return await self._call("POST", "/admin/api/simulate", json={"need": need, "roles": roles})

    async def stats(self, days: int) -> dict[str, Any]:
        return await self._call("GET", "/admin/api/stats", params={"days": days})

    async def evaluate(self) -> dict[str, Any]:
        return await self._call("POST", "/admin/api/eval", seconds=EVAL_TIMEOUT_SECONDS)

    async def eval_history(self) -> dict[str, Any]:
        return await self._call("GET", "/admin/api/eval/history")

    async def suggestions(self, days: int) -> dict[str, Any]:
        return await self._call(
            "GET", "/admin/api/suggestions", params={"days": days}, seconds=EVAL_TIMEOUT_SECONDS
        )

    async def decide(self, suggestion_id: str, decision: str) -> dict[str, Any]:
        if not _SUGGESTION_ID.match(suggestion_id) or decision not in ("accepted", "rejected"):
            raise MapServerError(_("Unknown suggestion."))
        return await self._call(
            "POST",
            f"/admin/api/suggestions/{suggestion_id}/decision",
            json={"decision": decision},
        )

