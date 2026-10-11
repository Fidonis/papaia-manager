"""The shared Jinja2 environment.

Page routes and the application-level error handlers both render templates.
Sharing one environment keeps a single template search path and one compiled
template cache, instead of each module standing up its own.

fidonis-brand: 2 -- the asset fingerprinting below is vendored verbatim into
Fidonis/qdrant-ingest's src/ui/templating.py. Keep the two in step; see that
repo's docs/ui.md.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from app.auth.oidc import OIDCClaims
from app.auth.roles import is_identity_admin
from app.config import get_settings
from app.core.ingest import display
from app.core.rag import rag_active
from app.core.settings_store import EffectiveBranding, effective_branding

templates = Jinja2Templates(directory="app/templates")

# Resolved from this module rather than the working directory: unlike the
# template search path above, this is read at request time, long after
# whatever cwd the process started in.
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# name -> (st_mtime_ns, st_size, url)
_ASSET_URLS: dict[str, tuple[int, int, str]] = {}


def asset_url(name: str) -> str:
    """Return a static asset's URL carrying a fingerprint of its content.

    `app.css` is generated *from the templates* at image build time, so a copy
    left in a browser cache pairs new markup with an older build's styling and
    silently drops whatever that markup relies on. Only a change of URL evicts
    it: the /static mount sends no Cache-Control, so browsers fall back to
    heuristic freshness and reuse the response without revalidating at all --
    an ETag never gets a chance to be checked.

    Keyed on the stat signature so an asset rebuilt under a running process is
    picked up without a restart, and re-hashed only when that signature moves.
    """
    path = _STATIC_DIR / name
    try:
        stat = path.stat()
    except OSError:
        return f"/static/{name}"

    cached = _ASSET_URLS.get(name)
    if cached is not None and (cached[0], cached[1]) == (stat.st_mtime_ns, stat.st_size):
        return cached[2]

    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return f"/static/{name}"

    url = f"/static/{name}?v={digest[:12]}"
    _ASSET_URLS[name] = (stat.st_mtime_ns, stat.st_size, url)
    return url


templates.env.globals["asset_url"] = asset_url


@pass_context
def branding(context: dict[str, object]) -> EffectiveBranding:
    """The configured sidebar branding, resolved against the defaults.

    A global rather than a value in each route's context: the error handlers
    render pages without going through the UI router's context builder, and
    every one of them still has to carry the brand. The settings lookup honours
    a dependency override so tests can point it at their own directory.
    """
    request = context.get("request")
    resolver = get_settings
    if isinstance(request, Request):
        resolver = request.app.dependency_overrides.get(get_settings, get_settings)
    return effective_branding(resolver().papaia_config_dir)


templates.env.globals["branding"] = branding


@pass_context
def rag_enabled(context: dict[str, object]) -> bool:
    """Whether the core runs the RAG system; what the sidebar category hangs off.

    A global for the same reason as `branding`: the sidebar is part of every admin page,
    including the ones rendered by an error handler, which never go through the UI router's
    context builder. The lookup is one small `.env` read.
    """
    request = context.get("request")
    resolver = get_settings
    if isinstance(request, Request):
        resolver = request.app.dependency_overrides.get(get_settings, get_settings)
    return rag_active(resolver().papaia_config_dir)


templates.env.globals["rag_enabled"] = rag_enabled


@pass_context
def users_nav(context: dict[str, object]) -> bool:
    """Whether the sidebar offers the Users page to the account looking at it.

    A global for the same reason as `rag_enabled`: the sidebar is part of every admin page,
    including the ones an error handler renders. Presentation only -- the routes enforce
    the role themselves. The page exists only where the accounts live in the bundled
    Keycloak.
    """
    claims = context.get("user")
    if not isinstance(claims, OIDCClaims):
        return False
    request = context.get("request")
    resolver = get_settings
    if isinstance(request, Request):
        resolver = request.app.dependency_overrides.get(get_settings, get_settings)
    settings = resolver()
    return settings.is_internal_keycloak and is_identity_admin(claims, settings)


templates.env.globals["users_nav"] = users_nav

# Times, spans and sizes of the ingest pages. The zone is passed in by the page, because it
# is the ingester's and not the manager's.
templates.env.filters["ingest_when"] = display.local_time
templates.env.filters["ingest_ago"] = display.ago
templates.env.filters["ingest_duration"] = display.duration
templates.env.filters["filesize"] = display.size_text
templates.env.filters["short_id"] = display.short_id
