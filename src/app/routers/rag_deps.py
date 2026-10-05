"""Dependencies shared by the RAG pages (Connections, Collections, Embedding) and their APIs."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Query, status

from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.ingest.client import IngestClient
from app.core.ingest.runs import EmbeddingService
from app.core.qdrant import QdrantClient, tls_verify
from app.core.rag import rag_active, rag_backend, rag_secrets
from app.core.rag_collections import CollectionStore
from app.core.vectordb import (
    CAPABILITY_COLLECTIONS,
    ProbeEnv,
    UnknownConnectionError,
    get_type,
)
from app.core.vectordb.service import DEFAULT_NAME, ConnectionService

_NO_API_KEY = (
    "QDRANT_JWT_SECRET is not set in ai/rag/.env, so the manager has no api-key for Qdrant."
)


def require_rag_admin(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> OIDCClaims:
    """An administrator, on a deployment that runs the RAG system.

    The profile is checked after the role on purpose: a signed-out browser must still
    get the login redirect and a non-admin the 403, and only an administrator learns
    that the page does not exist here.
    """
    if not rag_active(settings.papaia_config_dir):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The RAG system is not enabled on this deployment",
        )
    return user


RagAdmin = Annotated[OIDCClaims, Depends(require_rag_admin)]


def get_http_transport() -> httpx.AsyncBaseTransport | None:
    """The transport every connection to a vector database goes through.

    `None` is the network. Tests replace this dependency with a transport that answers
    for the databases they simulate.
    """
    return None


HttpTransport = Annotated[httpx.AsyncBaseTransport | None, Depends(get_http_transport)]


def get_connection_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConnectionService:
    return ConnectionService(settings)


ConnectionServiceDep = Annotated[ConnectionService, Depends(get_connection_service)]


def get_probe_env(
    settings: Annotated[Settings, Depends(get_settings)],
    transport: HttpTransport,
) -> ProbeEnv:
    return ProbeEnv(cafile=settings.ssl_cert_file, transport=transport)


async def get_store(
    settings: Annotated[Settings, Depends(get_settings)],
    service: ConnectionServiceDep,
    transport: HttpTransport,
    connection: Annotated[str, Query(max_length=64)] = DEFAULT_NAME,
) -> AsyncIterator[CollectionStore]:
    """A store on the selected connection, on a client that lives for one request.

    The connection comes from the query string and defaults to the default connection.
    A connection whose key is unusable is a store that explains why, not an error: it is
    a state of the page.
    """
    service.ensure_default()
    try:
        resolved = service.resolve(connection)
    except UnknownConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if CAPABILITY_COLLECTIONS not in get_type(resolved.type).capabilities:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The connection {resolved.name!r} does not support collections.",
        )

    blocked: str | None = None
    hint = f"Check the api-key of the connection {resolved.name!r} on the Connections page."
    if resolved.source == "env":
        # No stored default: the stack's own settings stand in for it.
        hint = "Check QDRANT_JWT_SECRET in ai/rag/.env."
        if not resolved.api_key:
            blocked = _NO_API_KEY
    elif resolved.key_state == "unreadable":
        blocked = (
            f"The api-key of the connection {resolved.name!r} cannot be decrypted with the "
            "current QI_CONNECTIONS_SECRET. Enter the key again on the Connections page."
        )
    elif resolved.integrated:
        hint += " Reset restores the stack's key."

    client = QdrantClient(
        resolved.url,
        resolved.api_key,
        verify=tls_verify(settings.ssl_cert_file),
        transport=transport,
        refused_hint=hint,
    )
    try:
        yield CollectionStore(
            client,
            rag_backend(settings.papaia_config_dir),
            connection=resolved.name,
            roles_enforced=resolved.integrated,
            blocked=blocked,
        )
    finally:
        await client.aclose()


async def get_ingest_client(
    settings: Annotated[Settings, Depends(get_settings)],
    transport: HttpTransport,
) -> AsyncIterator[IngestClient]:
    """A client for the ingester's REST control plane, for the length of one request.

    The token is read from the RAG module's `.env` now rather than at start, so a shell on
    the host and the manager agree on it. A missing token is a state the page explains, not
    an error here: the client refuses to send without one.
    """
    token = rag_secrets(settings.papaia_config_dir).ingest_api_token
    client = IngestClient(settings.qdrant_ingest_url, token, transport=transport)
    try:
        yield client
    finally:
        await client.aclose()


def get_embedding_service(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[CollectionStore, Depends(get_store)],
    client: Annotated[IngestClient, Depends(get_ingest_client)],
) -> EmbeddingService:
    return EmbeddingService(settings, store=store, client=client)


EmbeddingServiceDep = Annotated[EmbeddingService, Depends(get_embedding_service)]
