"""Dependencies shared by the Models page and its API."""
from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import Depends


def get_litellm_transport() -> httpx.AsyncBaseTransport | None:
    """The transport every call to LiteLLM goes through.

    `None` is the network. Tests replace this dependency with a transport that answers for the
    LiteLLM they simulate.
    """
    return None


LitellmTransport = Annotated[httpx.AsyncBaseTransport | None, Depends(get_litellm_transport)]
