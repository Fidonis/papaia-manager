"""The texts of the kit that the shared page modules show (static/js/core/errors.js).

A refused request is told in three parts (pattern 8 of the kit): what happened, what to do,
and the reference. The page modules sort a refusal into an error class of the kit; the texts
of each class are the kit's own (`papaia_kit.errors.display_text` with the code of the class),
in the language of the frame, so every module says "connection stopped", "right missing",
"input invalid" and "session expired" in the same words. base.html writes them into its data
island; nothing here reads a request beyond the language and the contact point.
"""

from __future__ import annotations

from typing import Any

from jinja2 import pass_context
from papaia_kit.errors import ErrorClass, display_text, load_catalog

from app import shell
from app.config import Settings, get_settings
from app.core.installation import contact_point
from app.i18n import runtime as i18n_runtime

# Any valid correlation id: display_text builds part 3 with it, which the page builds itself.
_SAMPLE_ID = "00000000-0000-7000-8000-000000000000"


def error_texts(lang: str, contact: str | None) -> dict[str, Any]:
    """Parts 1 and 2 of every error class in `lang`, and the label of part 3."""
    catalog = load_catalog()
    params = {"contact": contact} if contact else None
    classes: dict[str, dict[str, str]] = {}
    for error_class in ErrorClass:
        shown = display_text(
            catalog.class_codes[error_class], correlation_id=_SAMPLE_ID, lang=lang, params=params
        )
        classes[error_class.value] = {
            "what": shown.what,
            "consequence": shown.consequence or "",
            "todo": shown.todo,
        }
    return {"reference_label": catalog.reference_label[lang], "classes": classes}


def _settings(context: dict[str, object]) -> Settings:
    request = context.get("request")
    app = getattr(request, "app", None)
    overrides = getattr(app, "dependency_overrides", {}) or {}
    resolver = overrides.get(get_settings, get_settings)
    settings: Settings = resolver()
    return settings


@pass_context
def core_error_texts(context: dict[str, object]) -> dict[str, Any]:
    """`error_texts` for the page being rendered: the frame's language and contact point."""
    lang = shell.frame_language(i18n_runtime.locale_state().locale)
    contact = contact_point(_settings(context).papaia_config_dir)
    return error_texts(lang, contact["name"] if contact else None)
