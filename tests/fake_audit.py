"""Stand-ins for the permission report (OPS-19): the token endpoint of the realm for the
exchange to `papaia-audit`, and papaia-audit's `POST /v1/access-reports/build`.

Synthetic persons and systems only; tokens are unsigned test values that the kit's exchange
client reads the claims of (it never verifies a signature, papaia-audit does). Used by
tests/test_access_report.py and the stand-in world of the page tests (tests/design_world.py).
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from papaia_kit.exchange import ACCESS_TOKEN_TYPE

CLIENT_ID = "papaia-manager"
AUDIENCE = "papaia-audit"
SID = "a3c1e5f7-session-of-the-auditor"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt(claims: Mapping[str, Any]) -> str:
    """An unsigned compact token with these claims (the signature part is a fixed filler)."""
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    return f"{header}.{_b64(json.dumps(dict(claims)).encode())}.{_b64(b'not-a-signature')}"


def session_token(sub: str, *, client_id: str = CLIENT_ID) -> str:
    """The signed-in person's access token of the manager's own sign-in."""
    now = int(time.time())
    return jwt({
        "sub": sub, "azp": client_id, "sid": SID, "auth_time": now - 60,
        "exp": now + 300, "aud": "account", "marker": "subject-token-value",
    })  # fmt: skip


class FakeIssuer:
    """The realm's token endpoint for the exchange: records every form it was sent."""

    def __init__(self) -> None:
        self.forms: list[dict[str, str]] = []
        self.issued: list[str] = []
        self.mode = "ok"  # ok | refuse | down

    def post_form(
        self, url: str, form: Mapping[str, str], headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.forms.append(dict(form))
        if self.mode == "down":
            raise OSError("connection refused")
        if self.mode == "refuse":
            return 400, json.dumps({"error": "invalid_scope"}).encode()
        subject = json.loads(base64.urlsafe_b64decode(form["subject_token"].split(".")[1] + "=="))
        now = int(time.time())
        token = jwt({
            "sub": subject["sub"], "azp": CLIENT_ID, "aud": form["audience"],
            "sid": subject["sid"], "auth_time": subject["auth_time"], "exp": now + 300,
            "marker": f"exchanged-token-value-{len(self.issued)}",
        })  # fmt: skip
        self.issued.append(token)
        body = {
            "access_token": token, "expires_in": 300, "token_type": "Bearer",
            "issued_token_type": ACCESS_TOKEN_TYPE,
        }  # fmt: skip
        return 200, json.dumps(body).encode()


# -- reports -----------------------------------------------------------------------------


def _digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


_STATE = ("digest", "last_confirmed_at", "confirmation_due")
SESSIONS = {"offline": {"idle_days": 30, "max_age_days": 90}, "delegations": {"days": 30}}
ANNA = "5b0e8f0a-2c51-4d7e-9a43-1f6c2d8e9b10"
BEN = "c2d4f6a8-0b1c-4e3d-8f7a-6b5c4d3e2f10"
DORA = "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b"
BREAKGLASS = "f0e1d2c3-b4a5-4968-8776-655443322110"


def _person(sub: str, username: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": f"person:{sub}",
        "sub": sub,
        "username": username,
        "enabled": True,
        "breakglass": False,
        "realm_roles": ["user"],
        "module_roles": {},
        "groups": [],
        "service_accounts": {"owner": [], "deputy": []},
        "delegations": {},
        "separation_conflict": [],
    }
    row.update(extra)
    return row


def person_rows() -> list[dict[str, Any]]:
    return [
        _person(
            ANNA, "anna", realm_roles=["papaia-admin", "papaia-auditor", "user"],
            separation_conflict=["papaia-admin", "papaia-auditor"],
            groups=["leitung"], service_accounts={"owner": ["svc-flow-paperless"], "deputy": []},
        ),
        _person(
            BEN, "ben", module_roles={"papaia-studio": ["builder"]}, groups=["buchhaltung"],
            service_accounts={"owner": [], "deputy": ["svc-flow-paperless"]},
            delegations={"flow-engine": 2},
        ),
        _person(DORA, "dora", realm_roles=["papaia-auditor", "user"]),
        _person(BREAKGLASS, "breakglass", breakglass=True, enabled=False,
                realm_roles=["papaia-admin"]),
    ]  # fmt: skip


def system_rows() -> list[dict[str, Any]]:
    return [
        {
            "key": "system:mcp-paperless",
            "audience": "mcp-paperless",
            "exchange_clients": [
                {"client": "flow-engine", "effects": ["r", "w"], "requested_by": "ops-anna",
                 "confirmed_by": "ops-dora"},
            ],
            "service_accounts": [
                {"id": "svc-flow-paperless", "module": "flow", "effects": ["r", "w"]},
            ],
            "target_rights": "im Zielsystem geführt",
        },
        {
            "key": "system:papaia-audit",
            "audience": "papaia-audit",
            "exchange_clients": [
                {"client": "papaia-manager", "effects": ["r"], "requested_by": "platform",
                 "confirmed_by": "platform"},
            ],
            "service_accounts": [],
            "target_rights": "im Zielsystem geführt",
        },
    ]  # fmt: skip


# Keys of rows confirmed within the period; every other row is due.
CONFIRMED = {
    f"person:{DORA}": "2026-11-02T09:30:00Z",
    "system:papaia-audit": "2026-10-28T14:00:00Z",
    f"person:{BEN}": "2025-01-10T08:00:00Z",  # older than the period: due again
}
CURRENT = {f"person:{DORA}", "system:papaia-audit"}


def report(by: str, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A report of kind `by` as papaia-audit builds it: sorted rows, digests, checksum."""
    source = rows if rows is not None else (person_rows() if by == "person" else system_rows())
    built = []
    for row in sorted(source, key=lambda r: r["key"]):
        content = {k: v for k, v in row.items() if k not in _STATE}
        built.append({
            **content,
            "digest": _digest(content),
            "last_confirmed_at": CONFIRMED.get(row["key"]),
            "confirmation_due": row["key"] not in CURRENT,
        })  # fmt: skip
    contents = [{k: v for k, v in r.items() if k not in _STATE} for r in built]
    return {
        "format": "papaia.access-report/1",
        "by": by,
        "checksum": _digest({"sessions": SESSIONS, "rows": contents}),
        "recertification_days": 180,
        "inputs_written_at": "2026-11-20T07:00:00Z",
        "sessions": SESSIONS,
        "rows": built,
    }


_CSV = {
    "person": (
        "key", "sub", "username", "enabled", "breakglass", "realm_roles", "module_roles",
        "groups", "service_accounts_owner", "service_accounts_deputy", "delegations",
        "separation_conflict", "digest", "last_confirmed_at", "confirmation_due",
    ),
    "system": (
        "key", "audience", "exchange_clients", "service_accounts", "target_rights", "digest",
        "last_confirmed_at", "confirmation_due",
    ),
}  # fmt: skip


def render(doc: Mapping[str, Any], fmt: str) -> bytes:
    """The answer's bytes: indented JSON with sorted keys, or the CSV of papaia-audit."""
    if fmt == "json":
        return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(_CSV[doc["by"]])
    for row in doc["rows"]:
        writer.writerow([row["key"]] + [""] * (len(_CSV[doc["by"]]) - 1))
    return out.getvalue().encode()


class FakeAudit:
    """papaia-audit's build route: answers from `answers`, records every call."""

    PROBLEM_403 = {
        "class": "forbidden_or_not_found",
        "code": "AUDIT-REPORT-403",
        "message": "Der Berechtigungsbericht ist nur mit der Rolle Audit-Prüfung abrufbar.",
        "hint": "Bitten Sie die Verwaltung um die Rolle „Audit-Prüfung“.",
        "correlation_id": "00000000-0000-4000-8000-000000000000",
    }

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.mode = "ok"  # ok | refuse | down | broken | unavailable
        self.docs = {"person": report("person"), "system": report("system")}

    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, str, bytes]:
        request = json.loads(body)
        self.calls.append({"url": url, "body": request, "headers": dict(headers)})
        if urlsplit(url).path != "/v1/access-reports/build":
            return 404, "application/problem+json", b"{}"
        if self.mode == "down":
            raise OSError("connection refused")
        if self.mode == "refuse":
            return 403, "application/problem+json", json.dumps(self.PROBLEM_403).encode()
        if self.mode == "unavailable":
            problem = {
                "class": "upstream_unavailable", "code": "AUDIT-REPORT-INPUTS",
                "message": "Der Berechtigungsbericht ist auf diesem Server noch nicht "
                "eingerichtet.",
                "hint": "Führen Sie auf dem Host „papaia-ctl realm sync“ aus.",
            }  # fmt: skip
            return 503, "application/problem+json", json.dumps(problem).encode()
        doc = self.docs[request["by"]]
        if self.mode == "broken":
            doc = {**doc, "checksum": "sha256:" + "0" * 64}
        media = "application/json" if request["format"] == "json" else "text/csv"
        return 200, f"{media}; charset=utf-8", render(doc, request["format"])

    def answer(self, by: str, fmt: str) -> bytes:
        return render(self.docs[by], fmt)
