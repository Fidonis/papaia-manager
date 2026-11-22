"""The administration overview of delegations: how many per module and per person, no content.

A delegation is an offline session of a person at the client of its holder (sicherheit 1.10):
`flow-engine` for Flow, `assist-gate` for Assistant. Keycloak lists them per client; this
module counts them per person and passes on nothing else -- no session id, address, time,
purpose or token (PLT-58-A8). Revoking per person removes the person's consent at the holder's
client, which ends every delegation of that person there; the holder's next renewal fails and
its runs stop with `identity_revoked`.

The calls act with the signed-in administrator's own Keycloak token, so Keycloak decides
whether that account may list sessions and revoke consents.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.keycloak_users import KeycloakUsers

PAGE_SIZE = 100
# 50 pages of 100: beyond that the overview says it is incomplete instead of growing.
MAX_PAGES = 50

_USER_ID = re.compile(r"[0-9A-Fa-f-]{8,64}")


@dataclass(frozen=True, slots=True)
class Holder:
    module: str
    client_id: str
    title: str


HOLDERS: tuple[Holder, ...] = (
    Holder(module="flow", client_id="flow-engine", title="Flow"),
    Holder(module="assistant", client_id="assist-gate", title="Assistant"),
)
HOLDER_BY_MODULE: Mapping[str, Holder] = {h.module: h for h in HOLDERS}


def valid_user_id(value: str) -> bool:
    return _USER_ID.fullmatch(value) is not None


async def _count(client: KeycloakUsers, internal_id: str) -> tuple[dict[str, dict[str, Any]], bool]:
    persons: dict[str, dict[str, Any]] = {}
    for page in range(MAX_PAGES):
        rows = await client.offline_sessions(internal_id, first=page * PAGE_SIZE, limit=PAGE_SIZE)
        for row in rows:
            user_id, username = row.get("userId"), row.get("username")
            if not isinstance(user_id, str) or not valid_user_id(user_id):
                continue
            person = persons.setdefault(
                user_id,
                {
                    "user_id": user_id,
                    "username": username if isinstance(username, str) else "",
                    "count": 0,
                },
            )
            person["count"] += 1
        if len(rows) < PAGE_SIZE:
            return persons, False
    return persons, True


async def overview(client: KeycloakUsers) -> list[dict[str, Any]]:
    """Per holder: whether its client exists, the total, and the count per person."""
    result: list[dict[str, Any]] = []
    for holder in HOLDERS:
        internal_id = await client.client_internal_id(holder.client_id)
        persons: dict[str, dict[str, Any]] = {}
        truncated = False
        if internal_id is not None:
            persons, truncated = await _count(client, internal_id)
        ordered = sorted(persons.values(), key=lambda p: (p["username"].lower(), p["user_id"]))
        result.append(
            {
                "module": holder.module,
                "title": holder.title,
                "available": internal_id is not None,
                "total": sum(p["count"] for p in ordered),
                "truncated": truncated,
                "persons": ordered,
            }
        )
    return result
