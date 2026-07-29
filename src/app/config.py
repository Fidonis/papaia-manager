"""Application settings loaded from environment variables via pydantic-settings."""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
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
    manager_admin_role: str = "admin"
    # Realm role granting dashboard-only access. Admins implicitly have it too.
    manager_user_role: str = "user"
    manager_host: str
    manager_oidc_client_id: str = "papaia-manager"
    manager_oidc_client_secret: str
    manager_session_secret: str

    # Paths (must equal host paths when running in Docker)
    papaia_config_dir: str
    papaia_workspace_dir: str

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
