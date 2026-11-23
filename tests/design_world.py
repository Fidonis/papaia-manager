"""The stand-in installation the page tests and the design check render the manager in.

One configuration directory with the RAG profile on and a distributed topology, so every
page of `ui/pages.yaml` is reachable, signed session cookies for the test persons of the list
(Anna, an administrator of the platform; Emil, the auditor of the inventory, who is no
administrator: separation of duties), and the simulated Keycloak of the tests for the Users
and Roles pages. Nothing here talks to a real service: pages that would ask Docker, LiteLLM
or Qdrant show their own "unreachable" state. Used by tests/test_kit_design.py and
tests/design_serve.py, so the design check sees the pages the tests see.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

from fake_keycloak import TOKEN, FakeKeycloak
from fastapi import FastAPI
from itsdangerous import TimestampSigner

SESSION_COOKIE = "papaia_manager_session"
# The roles of Anna: manager administrator, dashboard user and platform administrator (the
# Users page needs the identity role of the bundled Keycloak).
ROLES = ("admin", "user", "papaia-admin")
# The test persons of ui/pages.yaml (column `person`) with their realm roles. Emil holds
# `papaia-auditor` (KE-13) and nothing of the administration, so the inventory is his page.
PERSONS: dict[str, tuple[str, ...]] = {
    "anna": ROLES,
    "emil": ("user", "papaia-auditor"),
}
CSRF = "design-check-csrf-token-0001"
# Values for the placeholders of ui/pages.yaml. The pages behind them render the state of
# an id the manager does not know, which is a page of the module as well.
SAMPLES = {
    "job_id": "0b5c4a1e-job",
    "name": "papaia-sample",
    "run_id": "0b5c4a1e-run",
}


def make_world(root: Path) -> tuple[Path, Path]:
    """Create the configuration and workspace directories under `root`."""
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
    return config, workspace


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


def stand_ins(app: FastAPI) -> FakeKeycloak:
    """Answer the Users and Roles pages from the simulated Keycloak, with synthetic accounts."""
    from app.routers.users_deps import get_keycloak_transport, get_user_token

    keycloak = FakeKeycloak(smtp=True)
    keycloak.add_user("anna", email="anna@example.test", first="Anna", roles=("papaia-admin",))
    keycloak.add_user("ben", email="ben@example.test", first="Ben", roles=("user",))
    keycloak.add_user("carla", first="Carla", roles=("user",), enabled=False)
    app.dependency_overrides[get_keycloak_transport] = keycloak.transport
    app.dependency_overrides[get_user_token] = lambda: TOKEN
    return keycloak
