"""Dependencies shared by the Users page and its API."""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status

from app.auth.deps import IdentityAdmin
from app.auth.oidc import OIDCClaims
from app.auth.user_token import UserTokenUnavailable, user_access_token
from app.config import Settings, get_settings
from app.core.keycloak_users import (
    KeycloakConflictError,
    KeycloakError,
    KeycloakForbiddenError,
    KeycloakNotFoundError,
    KeycloakRejectedError,
    KeycloakUnauthorizedError,
    KeycloakUnavailableError,
    KeycloakUsers,
    admin_endpoint,
)
from app.core.qdrant import tls_verify
from app.core.users_service import InvalidInput, UsersService

_NOT_KEYCLOAK = (
    "Users are managed in your identity provider: this deployment does not use the "
    "bundled Keycloak."
)
_NO_ENDPOINT = (
    "The manager cannot tell where the Keycloak Admin API is: OIDC_ISSUER_KC_TOKEN is not "
    "the token endpoint of a Keycloak realm."
)


def require_users_enabled(
    user: IdentityAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
) -> OIDCClaims:
    """The identity administrator, on a deployment whose accounts live in the bundled Keycloak.

    The deployment is checked after the role on purpose: a signed-out browser still gets the
    login redirect and an account without the role the 403, and only an identity
    administrator learns that the page does not exist here.
    """
    if not settings.is_internal_keycloak:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_KEYCLOAK)
    return user


UsersAdmin = Annotated[OIDCClaims, Depends(require_users_enabled)]


def get_keycloak_transport() -> httpx.AsyncBaseTransport | None:
    """The transport every call to Keycloak's Admin API goes through.

    `None` is the network. Tests replace this dependency with a transport that answers for
    a Keycloak they simulate.
    """
    return None


KeycloakTransport = Annotated[httpx.AsyncBaseTransport | None, Depends(get_keycloak_transport)]


async def get_user_token(request: Request, _user: UsersAdmin) -> str:
    """The access token of the signed-in account, from the refresh token of the session.

    A session that cannot produce one is over as far as Keycloak is concerned, so it is a
    401 and the browser signs in again.
    """
    try:
        return await user_access_token(request)
    except UserTokenUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired"
        ) from exc


UserToken = Annotated[str, Depends(get_user_token)]


async def get_users_service(
    user: UsersAdmin,
    token: UserToken,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: KeycloakTransport,
) -> AsyncIterator[UsersService]:
    """The Users page's actions on a client that lives for one request.

    The client acts with the signed-in account's own token, so what Keycloak lets it do is
    exactly what that account may do there.
    """
    endpoint = admin_endpoint(settings.oidc_issuer_kc_token)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_NO_ENDPOINT)
    client = KeycloakUsers(
        endpoint,
        token,
        verify=tls_verify(settings.ssl_cert_file),
        transport=transport,
    )
    try:
        yield UsersService(
            client,
            actor=user,
            identity_role=settings.manager_identity_admin_role,
            default_role=settings.manager_user_role,
        )
    finally:
        await client.aclose()


UsersServiceDep = Annotated[UsersService, Depends(get_users_service)]


@contextmanager
def translated(what: str = "") -> Iterator[None]:
    """Answer a failure of the Users API with the status code the other admin routes use."""
    try:
        yield
    except InvalidInput as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KeycloakUnauthorizedError as exc:
        raise HTTPException(status_code=401, detail="session expired") from exc
    except KeycloakForbiddenError as exc:
        raise HTTPException(
            status_code=403,
            detail=(
                "Keycloak refused this for your account"
                + (f": {exc.detail}" if exc.detail and not exc.detail.startswith("HTTP ") else ".")
            ),
        ) from exc
    except KeycloakNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"{what or 'that account'} was not found"
        ) from exc
    except KeycloakConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except KeycloakRejectedError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc
    except KeycloakError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc
