"""Internationalisation: gettext catalogues, the request's language, the Jinja2 wiring.

    from app.i18n import gettext as _, ngettext

    raise HTTPException(status_code=409, detail=_("The job is already running."))
    _("%(count)s file left") % {"count": n}      # named placeholders, never an f-string

See "Internationalisation" in AGENTS.md for what is translated, what is not, and how a language
is added.
"""
from __future__ import annotations

from app.i18n.catalog import SOURCE_LOCALE, LocaleInfo, get_catalogs
from app.i18n.runtime import (
    N_,
    current_locale,
    gettext,
    gettext_noop,
    locale_state,
    ngettext,
    npgettext,
    pgettext,
    use_locale,
)

__all__ = [
    "N_",
    "SOURCE_LOCALE",
    "LocaleInfo",
    "current_locale",
    "get_catalogs",
    "gettext",
    "gettext_noop",
    "locale_state",
    "ngettext",
    "npgettext",
    "pgettext",
    "use_locale",
]
