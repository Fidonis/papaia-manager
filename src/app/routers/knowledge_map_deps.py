"""Dependencies shared by the knowledge-map page and its API."""
from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import Depends


def get_knowledge_map_transport() -> httpx.AsyncBaseTransport | None:
    """The transport every call to the map server goes through.

    `None` is the network. Tests replace this dependency with a transport that answers for the
    map server they simulate.
    """
    return None


KnowledgeMapTransport = Annotated[
    httpx.AsyncBaseTransport | None, Depends(get_knowledge_map_transport)
]
