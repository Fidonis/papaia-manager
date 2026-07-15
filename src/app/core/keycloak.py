"""Idempotent Keycloak admin REST client for addon client registration."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ENV_PLACEHOLDER_RE = re.compile(r"\$\{env\.([A-Z0-9_]+)\}")


class KeycloakAdminClient:
    """Keycloak Admin REST client scoped to one realm.

    Uses a service-account token obtained via client_credentials on the
    papaia-manager client (serviceAccountsEnabled + manage-clients role).
    """

    def __init__(
        self,
        *,
        token_endpoint: str,
        admin_api_base: str,
        realm: str,
        client_id: str,
        client_secret: str,
        ssl_verify: bool | str = True,
        http_timeout: float = 15.0,
    ) -> None:
        self._token_endpoint = token_endpoint
        self._admin_base = f"{admin_api_base.rstrip('/')}/admin/realms/{realm}"
        self._client_id = client_id
        self._client_secret = client_secret
        self._ssl_verify = ssl_verify
        self._http_timeout = http_timeout

    async def _bearer(self) -> str:
        async with httpx.AsyncClient(
            timeout=self._http_timeout, verify=self._ssl_verify
        ) as client:
            resp = await client.post(
                self._token_endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            resp.raise_for_status()
            token: str = resp.json()["access_token"]
            return token

    async def register_client_idempotent(
        self,
        client_json_path: Path,
        bundle_env: dict[str, str],
    ) -> str:
        """Create a Keycloak client unless one with the same clientId already exists.

        Returns "created" or "exists".
        """
        raw = client_json_path.read_text(encoding="utf-8")  # noqa: ASYNC240
        doc: dict[str, Any] = __import__("json").loads(
            _substitute_env_placeholders(raw, bundle_env)
        )
        client_id = str(doc.get("clientId", ""))

        token = await self._bearer()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(
            timeout=self._http_timeout, verify=self._ssl_verify
        ) as client:
            search = await client.get(
                f"{self._admin_base}/clients",
                params={"clientId": client_id},
                headers=headers,
            )
            search.raise_for_status()
            existing = search.json()

        if existing:
            logger.info("keycloak client %r already exists — skipping", client_id)
            return "exists"

        async with httpx.AsyncClient(
            timeout=self._http_timeout, verify=self._ssl_verify
        ) as client:
            create = await client.post(
                f"{self._admin_base}/clients",
                json=doc,
                headers=headers,
            )
            create.raise_for_status()

        logger.info("created keycloak client %r", client_id)
        return "created"

    async def add_protocol_mapper_idempotent(
        self,
        *,
        target_client_id: str,
        mapper: dict[str, Any],
    ) -> str:
        """Add a protocol mapper to an existing client unless it already exists.

        Returns "created" or "exists".
        """
        mapper_name = str(mapper.get("name", ""))
        token = await self._bearer()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(
            timeout=self._http_timeout, verify=self._ssl_verify
        ) as client:
            search = await client.get(
                f"{self._admin_base}/clients",
                params={"clientId": target_client_id},
                headers=headers,
            )
            search.raise_for_status()
            clients = search.json()

        if not clients:
            raise RuntimeError(f"target client {target_client_id!r} not found in realm")

        kc_uuid = str(clients[0]["id"])

        async with httpx.AsyncClient(
            timeout=self._http_timeout, verify=self._ssl_verify
        ) as client:
            existing_resp = await client.get(
                f"{self._admin_base}/clients/{kc_uuid}/protocol-mappers/models",
                headers=headers,
            )
            existing_resp.raise_for_status()
            existing_names = {m.get("name") for m in existing_resp.json()}

        if mapper_name in existing_names:
            logger.info("protocol mapper %r on %r already exists", mapper_name, target_client_id)
            return "exists"

        async with httpx.AsyncClient(
            timeout=self._http_timeout, verify=self._ssl_verify
        ) as client:
            resp = await client.post(
                f"{self._admin_base}/clients/{kc_uuid}/protocol-mappers/models",
                json=mapper,
                headers=headers,
            )
            resp.raise_for_status()

        logger.info("added protocol mapper %r to %r", mapper_name, target_client_id)
        return "created"


def _substitute_env_placeholders(text: str, bundle_env: dict[str, str]) -> str:
    """Replace ``${env.KEY}`` placeholders with values from bundle_env."""
    def replace(m: re.Match[str]) -> str:
        key = m.group(1)
        return bundle_env.get(key, m.group(0))

    return _ENV_PLACEHOLDER_RE.sub(replace, text)
