"""The shared Jinja2 environment.

Page routes and the application-level error handlers both render templates.
Sharing one environment keeps a single template search path and one compiled
template cache, instead of each module standing up its own.

fidonis-brand: 1 -- the asset fingerprinting below is vendored verbatim into
Fidonis/qdrant-ingest's src/ui/templating.py. Keep the two in step; see that
repo's docs/ui.md.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

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
