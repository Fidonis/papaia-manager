"""Application settings loaded from environment variables via pydantic-settings."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # OIDC endpoints
    oidc_issuer_kc_auth: str
    oidc_issuer_kc_token: str
    oidc_issuer_kc_certs: str
    oidc_role_claim: str = "roles"
    auth_provider: str = "internal_keycloak"

    # Manager application
    # Realm role granting full access (add-ons, catalogs, jobs and dashboard).
    manager_admin_role: str = "manager-admin"
    # Realm role granting dashboard-only access. Admins implicitly have it too.
    manager_user_role: str = "user"
    # Realm role that may manage the accounts and roles of the realm (the Users page). It is
    # not the admin role: the manager calls Keycloak's Admin API with the token of the
    # signed-in user, so this role is also the one that has to carry Keycloak's own
    # user-administration rights (`realm-management`: manage-users and friends).
    manager_identity_admin_role: str = "papaia-admin"
    manager_host: str
    manager_oidc_client_id: str = "papaia-manager"
    manager_oidc_client_secret: str
    manager_session_secret: str

    # Paths (must equal host paths when running in Docker)
    papaia_config_dir: str
    papaia_workspace_dir: str

    # Where this process reaches the Qdrant of the core's `rag` profile. The service name
    # resolves on the network the manager shares with it. It is the manager's own view:
    # the connection store, which the ingester reads, always holds the address the
    # ingester uses, and this replaces it only when the manager connects (see
    # app/core/vectordb/service.py). The api-key is not a setting: it is read from the
    # RAG module's `.env` at request time, next to the collection and role names (see
    # app/core/rag.py), so a shell on the host and the manager agree on it.
    qdrant_url: str = "http://qdrant:6333"

    # Where this process reaches the ingester's REST control plane, on the same network.
    # The static bearer token (`QI_API_TOKEN`) is read from the RAG module's `.env` at
    # request time like the other secrets (see app/core/rag.py). Not the public URL of the
    # ingester's web interface, which is the browser's address for the sign-in.
    qdrant_ingest_url: str = "http://qdrant-ingest:8300"

    # The Embedding page's upload area. An upload lives in a staging folder of its own that
    # is removed after a successful run; one that is left (a failed run, a forgotten upload)
    # is removed after this many hours. The sizes are per file and per upload, in MiB; the
    # ingester skips a file larger than its own `QI_MAX_FILE_BYTES` (200 MiB by default).
    ingest_upload_ttl_hours: int = Field(default=24, ge=1, le=720)
    ingest_max_upload_mb: int = Field(default=200, ge=1, le=10_240)
    ingest_max_batch_mb: int = Field(default=2_048, ge=1, le=102_400)

    # TLS (optional — path to custom CA bundle)
    ssl_cert_file: str | None = None

    # Logging
    log_level: str = "INFO"

    @field_validator("manager_host")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def oidc_redirect_uri(self) -> str:
        return f"{self.manager_host}/auth/callback"

    @property
    def is_internal_keycloak(self) -> bool:
        return self.auth_provider == "internal_keycloak"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
