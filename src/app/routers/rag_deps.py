"""Dependencies shared by the Collections page and its API."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.qdrant import QdrantClient
from app.core.rag import rag_active, rag_backend
from app.core.rag_collections import CollectionStore


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


async def get_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[CollectionStore]:
    """A store on a client that lives for one request.

    Tests replace this dependency with a store on a fake Qdrant.
    """
    backend = rag_backend(settings.papaia_config_dir)
    client = QdrantClient(
        settings.qdrant_url,
        backend.api_key,
        verify=settings.ssl_cert_file or True,
    )
    try:
        yield CollectionStore(client, backend)
    finally:
        await client.aclose()
