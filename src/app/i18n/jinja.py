"""Wires the translation functions into the shared Jinja2 environment."""
from __future__ import annotations

from jinja2 import Environment

from app.i18n import runtime
from app.i18n.catalog import LocaleInfo, get_catalogs
from app.i18n.negotiate import LocaleState


def html_lang() -> str:
    """The value of `<html lang>`: a BCP 47 tag, so `pt_BR` becomes `pt-BR`."""
    return runtime.current_locale().replace("_", "-")


def available_locales() -> list[LocaleInfo]:
    return get_catalogs().locales


def locale_name(code: str) -> str:
    """The name of a shipped language in that language; the code itself when unknown."""
    for info in get_catalogs().locales:
        if info.code == code:
            return info.name
    return code


def install(env: Environment) -> None:
    """Switch the i18n extension on and point it at the request's language.

    `newstyle` gettext: `{{ _("Hello %(name)s", name=user) }}` and `{% trans %}` escape their
    variables and fill the placeholders after the lookup. The consequence is that a literal
    percent sign in a template text has to be written `%%`.

    `trimmed` collapses the whitespace of a `{% trans %}` block, so the line breaks of the
    markup around a sentence do not end up in its message id.
    """
    env.add_extension("jinja2.ext.i18n")
    env.policies["ext.i18n.trimmed"] = True
    env.install_gettext_callables(  # type: ignore[attr-defined]
        runtime.gettext,
        runtime.ngettext,
        newstyle=True,
        pgettext=runtime.pgettext,
        npgettext=runtime.npgettext,
    )
    env.globals["html_lang"] = html_lang
    env.globals["current_locale"] = runtime.current_locale
    env.globals["locale_state"] = runtime.locale_state
    env.globals["available_locales"] = available_locales
    env.globals["locale_name"] = locale_name


__all__ = ["LocaleState", "available_locales", "html_lang", "install", "locale_name"]
