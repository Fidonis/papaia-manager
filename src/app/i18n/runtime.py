"""The language of the request being served, and the functions that translate with it.

The language is a `ContextVar` set once per request by `LocaleMiddleware`. It is not read from
the Jinja context: a macro imported without `with context` cannot see the request, and a
message raised in a router or a service has no template context at all. A `ContextVar` is
visible to both, and to the worker threads FastAPI runs synchronous handlers in.

Outside a request (a job, the scheduler, a notification) nothing sets it, so the text stays in
the language the source is written in.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from gettext import NullTranslations

from app.i18n.catalog import SOURCE_LOCALE, get_catalogs
from app.i18n.negotiate import LocaleState

_DEFAULT = LocaleState(SOURCE_LOCALE)
_state: ContextVar[LocaleState] = ContextVar("papaia_locale")


def locale_state() -> LocaleState:
    return _state.get(_DEFAULT)


def current_locale() -> str:
    return _state.get(_DEFAULT).locale


def set_state(state: LocaleState) -> Token[LocaleState]:
    return _state.set(state)


def reset_state(token: Token[LocaleState]) -> None:
    _state.reset(token)


@contextmanager
def use_locale(code: str) -> Iterator[None]:
    """Run a block in a language; for code outside a request, and for tests."""
    token = _state.set(LocaleState(code, stored=code, auto=code))
    try:
        yield
    finally:
        _state.reset(token)


def _translations() -> NullTranslations:
    return get_catalogs().translations(current_locale())


def gettext(message: str) -> str:
    """The message in the request's language; the message itself when there is no translation."""
    return _translations().gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translations().ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    """For a short word that means different things in different places ("Open", "Run")."""
    return _translations().pgettext(context, message)


def npgettext(context: str, singular: str, plural: str, n: int) -> str:
    return _translations().npgettext(context, singular, plural, n)


def gettext_noop(message: str) -> str:
    """Mark a text for extraction and return it unchanged.

    For a text that sits in a table built when the module is imported: translating it there
    would fix it in English for good. The code that shows it calls `gettext` on it instead.
    """
    return message


# Babel's extraction knows this spelling out of the box.
N_ = gettext_noop
