"""The `qdrant` connection type.

A Qdrant connection is an address and an optional api-key. In the ingester's store that
is `{name, url, api_key?}` and nothing else, and the type is not written down at all: an
entry without a type is a Qdrant. That keeps the file readable by every ingester that
exists, and a type added later is the one that has to say so.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from app.core.qdrant import QdrantClient, QdrantError, QdrantUnavailable, tls_verify
from app.core.vectordb.base import (
    CAPABILITY_COLLECTIONS,
    Connection,
    FieldSpec,
    ProbeEnv,
    ProbeResult,
)
from app.core.vectordb.errors import InvalidConnectionError
from app.i18n import N_, ngettext
from app.i18n import gettext as _

MAX_URL_LENGTH = 2048


def validate_url(value: str) -> str:
    """An address worth storing, beyond what the ingester itself insists on.

    The ingester only checks the scheme. The manager also refuses what would put a
    secret into the file in the clear (credentials in the address), what cannot be a
    host, and anything that is not one line of printable characters.
    """
    url = (value or "").strip()
    if not url:
        raise InvalidConnectionError(_("The address is required."))
    if len(url) > MAX_URL_LENGTH:
        raise InvalidConnectionError(_(
            "The address is at most %(MAX_URL_LENGTH)s characters."
        ) % {"MAX_URL_LENGTH": MAX_URL_LENGTH})
    if not url.startswith(("http://", "https://")):
        raise InvalidConnectionError(_("The address must start with http:// or https://."))
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise InvalidConnectionError(
            _("The address must not contain spaces or control characters.")
        )
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        raise InvalidConnectionError(_("The address is not a valid URL.")) from None
    if not parts.hostname:
        raise InvalidConnectionError(_("The address has no host."))
    if "@" in parts.netloc:
        raise InvalidConnectionError(
            _("The address must not contain credentials. Use the api-key field.")
        )
    return url


class QdrantType:
    id = "qdrant"
    label = "Qdrant"
    fields: tuple[FieldSpec, ...] = (
        FieldSpec(
            "url",
            N_("Address"),
            "url",
            required=True,
            help=N_("Base address of the Qdrant REST API, as the ingester reaches it."),
            placeholder="http://qdrant:6333",
        ),
        FieldSpec(
            "api_key",
            N_("API key"),
            "secret",
            help=N_("Leave empty for a Qdrant that does not ask for one."),
        ),
    )
    capabilities = frozenset({CAPABILITY_COLLECTIONS})

    def validate(self, values: Mapping[str, str]) -> dict[str, str]:
        return {"url": validate_url(values.get("url", ""))}

    def values_of(self, entry: Mapping[str, Any]) -> dict[str, str]:
        url = entry.get("url")
        return {"url": url if isinstance(url, str) else ""}

    def address_of(self, values: Mapping[str, str]) -> str:
        return values.get("url", "")

    def to_entry(
        self, name: str, values: Mapping[str, str], api_key_token: str | None
    ) -> dict[str, Any]:
        # Only the keys the ingester's schema allows. Anything else makes it refuse the
        # whole file.
        entry: dict[str, Any] = {"name": name, "url": values["url"]}
        if api_key_token:
            entry["api_key"] = api_key_token
        return entry

    async def probe(self, connection: Connection, env: ProbeEnv) -> ProbeResult:
        client = QdrantClient(
            connection.url,
            connection.api_key,
            verify=tls_verify(env.cafile),
            timeout=env.timeout,
            transport=env.transport,
            refused_hint=_(
                "Check the api-key of the connection %(name)r."
            ) % {"name": connection.name},
        )
        try:
            result = await client.request("GET", "/collections")
        except QdrantUnavailable as exc:
            return ProbeResult(False, exc.detail)
        except QdrantError as exc:
            return ProbeResult(False, _(
                "Qdrant answered with an error: %(detail)s"
            ) % {"detail": exc.detail})
        finally:
            await client.aclose()
        listed = result.get("collections") if isinstance(result, dict) else None
        count = len(listed) if isinstance(listed, list) else None
        if count is None:
            return ProbeResult(True, _("Reachable"), count)
        reachable = ngettext(
            "Reachable, %(count)s collection", "Reachable, %(count)s collections", count
        )
        return ProbeResult(True, reachable % {"count": count}, count)
