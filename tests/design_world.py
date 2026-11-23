"""The stand-in installation the page tests and the design check render the manager in.

One configuration directory with the RAG profile on and a distributed topology, so every
page of `ui/pages.yaml` is reachable, signed session cookies for the test persons of the list
(Anna, an administrator of the platform; Emil, the auditor of the inventory, who is no
administrator: separation of duties; Erika, the second person of the data subject requests),
the simulated Keycloak of the tests for the Users and Roles pages and a simulated papaia-audit
with synthetic requests for the page "Data subject requests". Nothing here talks to a real
service: pages that would ask Docker, LiteLLM or Qdrant show their own "unreachable" state.
Used by tests/test_kit_design.py and tests/design_serve.py, so the design check sees the
pages the tests see.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import fake_audit
import yaml
from fake_keycloak import TOKEN, FakeKeycloak
from fastapi import FastAPI
from itsdangerous import TimestampSigner

SESSION_COOKIE = "papaia_manager_session"
# The roles of Anna: manager administrator, dashboard user and platform administrator (the
# Users page needs the identity role of the bundled Keycloak).
ROLES = ("admin", "user", "papaia-admin")
# The test persons of ui/pages.yaml (column `person`) with their realm roles. Emil holds
# `papaia-auditor` (KE-13) and nothing of the administration, so the inventory and the
# permission report are his pages.
# Erika holds `papaia-confirm` and nothing of the administration: she is the second person who
# confirms requests for information and erasure, so the page "Data subject requests" is hers.
PERSONS: dict[str, tuple[str, ...]] = {
    "anna": ROLES,
    "emil": ("user", "papaia-auditor"),
    "erika": ("user", "papaia-confirm"),
}
CSRF = "design-check-csrf-token-0001"
# Values for the placeholders of ui/pages.yaml. The pages behind them render the state of
# an id the manager does not know, which is a page of the module as well.
SAMPLES = {
    "job_id": "0b5c4a1e-job",
    "name": "papaia-sample",
    "run_id": "0b5c4a1e-run",
}


def make_world(root: Path, *, rag_catalog: bool = False) -> tuple[Path, Path]:
    """Create the configuration and workspace directories under `root`.

    With `rag_catalog` the catalog directory of the RAG module exists, so the connection store
    of the Connections page can be changed (the paths of
    tests/test_connections_notifications_scripts.py); without it the page is read-only.
    """
    config = root / "config"
    workspace = root / "workspace"
    config.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    # The core's own file: the profile switch the RAG pages hang off, and the host the
    # dashboard's tiles link to (a reserved name that resolves nowhere).
    (config / ".env").write_text(
        "COMPOSE_PROFILES=rag\nPAPAIA_HOST=https://papaia.example.test\n", encoding="utf-8"
    )
    # A distributed installation of one node, the controller on this host, so the Nodes pages
    # exist; its state is read from a core that is not there.
    (config / "deployment.yaml").write_text(
        "topology:\n"
        "  mode: distributed\n"
        f"  controller: {SAMPLES['name']}\n"
        "  nodes:\n"
        f"    {SAMPLES['name']}:\n"
        "      address: 127.0.0.1\n"
        "      executor: local\n",
        encoding="utf-8",
    )
    if rag_catalog:
        (config / "ai" / "rag" / "catalog").mkdir(parents=True, exist_ok=True)
    return config, workspace


def add_mail_relay(config: Path, workspace: Path) -> None:
    """A core that ships the mail relay, switched on and set up with a synthetic server
    without a stored password, so the Mail page shows its form and the test mail
    (tests/test_settings_mail_browser.py). The relay itself is not there; its status shows
    "unreachable"."""
    lib = workspace / "papaia" / "tools" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "smtp.py").write_text("", encoding="utf-8")
    env = config / ".env"
    profiles = env.read_text(encoding="utf-8")
    profiles = profiles.replace("COMPOSE_PROFILES=rag\n", "COMPOSE_PROFILES=rag,smtp\n")
    env.write_text(profiles, encoding="utf-8")
    module = config / "infra" / "smtp-relay"
    module.mkdir(parents=True, exist_ok=True)
    (module / ".env").write_text(
        "SMTP_RELAY_UPSTREAM_HOST=smtp.example.org\n"
        "SMTP_RELAY_UPSTREAM_PORT=587\n"
        "SMTP_RELAY_UPSTREAM_SECURITY=starttls\n"
        "SMTP_RELAY_UPSTREAM_USER=mailer\n"
        "SMTP_RELAY_FROM_ADDRESS=noreply@example.org\n"
        "SMTP_RELAY_FROM_NAME=Example Mail\n",
        encoding="utf-8",
    )


# Restore points of the stand-in world with backups (design_serve.py --with-backups): one of
# the whole stack with a manifest, one that holds a single add-on only.
BACKUPS = (
    {
        "id": "2026-07-30_10-19-38",
        "created_at": "2026-07-30T10:19:38Z",
        "size_mb": 12.0,
        "result": "ok",
        "artifacts": 2,
        "addons": [],
    },
    {
        "id": "2026-07-31_11-00-00",
        "created_at": "2026-07-31T11:00:00Z",
        "size_mb": 3.5,
        "result": "partial",
        "artifacts": 1,
        "addons": ["papaia-sample"],
        "scope": ["addon:papaia-sample"],
    },
)


def seed_backups(config: Path, root: Path) -> Path:
    """Give the world a backup directory with the restore points of BACKUPS; returns it."""
    backup_dir = root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for point in BACKUPS:
        path = backup_dir / str(point["id"])
        path.mkdir(exist_ok=True)
        entries.append(
            {**point, "path": str(path), "papaia_version": "1.10.0", "project": "papaia"}
        )
    (backup_dir / str(BACKUPS[0]["id"]) / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "artifacts": [
                    {"kind": "config", "target": "config", "owner": "core"},
                    {"kind": "volume", "target": "papaia_librechat_mongo", "owner": "librechat"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (backup_dir / "backup.yaml").write_text(
        yaml.safe_dump({"version": 1, "backups": entries}), encoding="utf-8"
    )
    with (config / ".env").open("a", encoding="utf-8") as env:
        env.write(f"PAPAIA_BACKUP_DIR={backup_dir}\n")
    return backup_dir


def environment(config: Path, workspace: Path, *, host: str, secret: str) -> dict[str, str]:
    """The settings of the manager for the stand-in world (never a real identity provider)."""
    return {
        "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
        "OIDC_ISSUER_KC_TOKEN": "https://kc.test/realms/papaia/protocol/openid-connect/token",
        "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
        "MANAGER_ADMIN_ROLE": "admin",
        "MANAGER_USER_ROLE": "user",
        "MANAGER_HOST": host,
        "MANAGER_OIDC_CLIENT_SECRET": "design-check-client",
        "MANAGER_SESSION_SECRET": secret,
        "PAPAIA_CONFIG_DIR": str(config),
        "PAPAIA_WORKSPACE_DIR": str(workspace),
    }


def session_cookie(
    secret: str, roles: tuple[str, ...] = ROLES, person: str = "anna", **extra: Any
) -> str:
    """The signed session of `person` (Anna by default) with `roles`, as the manager reads it."""
    session: dict[str, Any] = {
        "user": {
            "sub": f"u-{person}",
            "preferred_username": person,
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": CSRF,
        **extra,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    return TimestampSigner(secret).sign(payload).decode()


def sample_path(pattern: str) -> str:
    """A page path of ui/pages.yaml with its placeholders filled from SAMPLES."""
    path = pattern
    for key, value in SAMPLES.items():
        path = path.replace("{" + key + "}", value)
    if "{" in path:
        raise KeyError(f"no sample value for a placeholder of {pattern}")
    return path


# Synthetic requests of the simulated papaia-audit, as Erika sees them (pseudonyms only): an
# open export she confirms, an open erasure with hand-over, one she filed herself, one done.
PERSON_REQUESTS: list[dict[str, Any]] = [
    {
        "id": request_id, "operation": operation, "state": state,
        "requested_at": "2026-11-23T08:15:00.000Z",
        "decided_at": "2026-11-22T10:00:00.000Z" if state == "done" else None,
        "person": "P-ANNAAAAA", "requested_by": "P-ERIKAAAA" if own else "P-DORAAAAA",
        "confirmed_by": "P-DORAAAAA" if own else "P-ERIKAAAA", "transfer": transfer,
        "own": own, "can_confirm": state == "open" and not own,
    }
    for request_id, operation, state, transfer, own in (
        ("0b5c4a1e-1111-4aaa-8bbb-000000000001", "export", "open", False, False),
        ("0b5c4a1e-2222-4aaa-9bbb-000000000002", "erase", "open", True, False),
        ("0b5c4a1e-3333-4aaa-abbb-000000000003", "export", "open", False, True),
        ("0b5c4a1e-4444-4aaa-bbbb-000000000004", "erase", "done", False, False),
    )
]  # fmt: skip


def _audit_send(method: str, url: str, headers: Any) -> tuple[int, bytes]:
    """papaia-audit's list of requests; this world decides nothing."""
    if method == "GET" and url.endswith("/v1/person-requests"):
        return 200, json.dumps({"requests": PERSON_REQUESTS}).encode()
    return 404, b'{"code": "AUDIT-READ-404"}'


def stand_ins(app: FastAPI) -> FakeKeycloak:
    """Answer the Users and Roles pages from the simulated Keycloak, with synthetic accounts,
    the page "Data subject requests" from the simulated papaia-audit and the permission report
    from a stand-in papaia-audit (tests/fake_audit.py)."""
    from papaia_kit.exchange import ClientSecret, ExchangeClient

    from app.core import person_requests
    from app.core.access_report import ReportService
    from app.routers.ui_access_report import get_report_service, get_subject_token
    from app.routers.ui_person_requests import person_token
    from app.routers.users_deps import get_keycloak_transport, get_user_token

    person_requests.use(
        person_requests.Wiring(
            base_url="https://papaia-audit.example.test:8443",
            send=_audit_send,
            exchange=lambda token, cid: token,
        )
    )
    app.dependency_overrides[person_token] = lambda: TOKEN

    keycloak = FakeKeycloak(smtp=True)
    keycloak.add_user("anna", email="anna@example.test", first="Anna", roles=("papaia-admin",))
    keycloak.add_user("ben", email="ben@example.test", first="Ben", roles=("user",))
    keycloak.add_user("carla", first="Carla", roles=("user",), enabled=False)
    app.dependency_overrides[get_keycloak_transport] = keycloak.transport
    app.dependency_overrides[get_user_token] = lambda: TOKEN

    audit = fake_audit.FakeAudit()
    report = ReportService(
        exchange=ExchangeClient(
            token_endpoint="https://kc.test/realms/papaia/protocol/openid-connect/token",
            client_id=fake_audit.CLIENT_ID,
            auth=ClientSecret("design-check-client"),
            transport=fake_audit.FakeIssuer(),
        ),
        post=audit.post,
        base_url="https://papaia-audit:8443",
    )

    async def subject(request: object) -> str:
        return fake_audit.session_token(fake_audit.DORA)

    app.dependency_overrides[get_report_service] = lambda: report
    app.dependency_overrides[get_subject_token] = lambda: subject
    return keycloak
