"""Internationalisation: gettext catalogues, the request's language, the Jinja2 wiring.

    from app.i18n import gettext as _, ngettext

    raise HTTPException(status_code=409, detail=_("The job is already running."))
    _("%(count)s file left") % {"count": n}      # named placeholders, never an f-string

The interface is English in the source and German in `app/locale/de/`. A language is a
directory, `app/locale/<code>/LC_MESSAGES/messages.po`, discovered at start-up (`catalog.py`),
listed in the account menu under its own name and used for `Accept-Language` matching (`de-AT`
finds `de`). No code, list or setting names the languages; the source language `en` needs no
catalogue. Catalogues are read with Babel and compiled in memory, without a `.mo` build step.

Which language a request gets (`middleware.py`, `negotiate.py`): the account's stored choice,
else the browser's `Accept-Language`, else `MANAGER_DEFAULT_LOCALE`, else `en`. The choice is
stored by OIDC `sub` in `$PAPAIA_CONFIG_DIR/manager/preferences.yaml` (`core/user_prefs.py`), so
it follows the account to every browser and travels with a backup. The language is a
`ContextVar` set by `LocaleMiddleware`, which is why it is also right in macros imported without
context, in exception handlers, in HTMX partials and in `/api/*` error messages.

Rules when writing a text the user reads:

- Templates: prose in `{% trans %}...{% endtrans %}` (plurals with `{% pluralize %}`, a variable
  named in the tag); an attribute or an expression uses `{{ _('...') }}`; for Alpine and
  JavaScript use `{{ _('...')|tojson|forceescape }}`. In `_()` a literal `%` is written `%%`.
- JavaScript gets its messages from the `I18N` object in `base.html` or from `tojson`, with
  `{name}` placeholders filled by `fmt`, `fmtHtml` and `fmtPlural`.
- Python translates where the text is produced, with named placeholders and never an f-string.
  A message in an exception or an `HTTPException` is translated where it is raised.
- Tables at module level use `N_()` and are translated where they are shown;
  `tests/test_i18n_static.py` fails on `_()` that runs at import time.
- Caches read by several people never hold a translated text.
- Not translated, on purpose: log lines, audit parameters, job titles and job logs, notification
  emails and webhooks, upgrade runner markers, anything the operator or an add-on catalog
  supplies, and what Keycloak or `papaia-ctl` return. Code outside a request runs in English.
- Never build a sentence from translated words; give each variant its own whole sentence.

`tests/test_i18n_*.py` keep this honest: every shipped catalogue is complete and current, the
templates carry no untranslated prose, nothing is translated at import time, and every page
renders in German. Adding a language: see "Add or update a language" in `CONTRIBUTING.md`.
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
