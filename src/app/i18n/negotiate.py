"""Choosing a language: the stored preference, then the browser, then the default."""
from __future__ import annotations

from dataclasses import dataclass

from app.i18n.catalog import SOURCE_LOCALE, Catalogs

# An absurdly long header is not a language list; stop reading it.
_MAX_HEADER = 1024
_MAX_TAGS = 32


def parse_accept_language(header: str | None) -> list[str]:
    """The language tags of an `Accept-Language` header, best first.

    Tags with `q=0` are refused languages and the wildcard names none, so both are left out.
    A malformed part is skipped; the header is client input and must never raise.
    """
    if not header:
        return []
    ranked: list[tuple[float, int, str]] = []
    for index, part in enumerate(header[:_MAX_HEADER].split(",")):
        tag, _, params = part.partition(";")
        tag = tag.strip()
        if not tag or tag == "*":
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        ranked.append((quality, index, tag))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [tag for _, _, tag in ranked[:_MAX_TAGS]]


def negotiate(header: str | None, catalogs: Catalogs) -> str | None:
    """The first shipped language the browser asks for, or None."""
    for tag in parse_accept_language(header):
        found = catalogs.match(tag)
        if found is not None:
            return found
    return None


@dataclass(frozen=True, slots=True)
class LocaleState:
    """The language of one request, and how it was reached."""

    # What the request is rendered in.
    locale: str
    # The language the account chose, or None for "automatic".
    stored: str | None = None
    # What "automatic" yields for this browser: what the account gets without a choice.
    auto: str = SOURCE_LOCALE


def resolve(
    *,
    stored: str | None,
    accept_language: str | None,
    default: str,
    catalogs: Catalogs,
) -> LocaleState:
    """Stored choice, else the browser's list, else the installation default, else English."""
    fallback = catalogs.match(default) or SOURCE_LOCALE
    auto = negotiate(accept_language, catalogs) or fallback
    chosen = catalogs.match(stored) if stored else None
    return LocaleState(locale=chosen or auto, stored=chosen, auto=auto)
