"""Reading the navigation of the kit's frame in a rendered page, for the page tests."""
from __future__ import annotations

import html
import re

_HEADING = re.compile(r'<h2 id="pk-nav-g\d+" class="pk-nav-group">([^<]+)</h2>')
_LABEL = re.compile(r'<span class="pk-label">([^<]+?)\s*(?:<|</span>)')


def nav_section(body: str) -> str:
    """The frame's navigation zone (`nav.pk-nav`), without the breadcrumb further down."""
    start = body.index('<nav class="pk-nav"')
    return body[start : body.index("</nav>", start)]


def nav_groups(body: str) -> dict[str, list[str]]:
    """Heading -> the rows under it, in order; the fixed first row (the overview) is ''."""
    parts = _HEADING.split(nav_section(body))
    groups = {"": [html.unescape(x) for x in _LABEL.findall(parts[0])]}
    for heading, chunk in zip(parts[1::2], parts[2::2], strict=True):
        groups[html.unescape(heading)] = [html.unescape(x) for x in _LABEL.findall(chunk)]
    return groups


def current_rows(body: str) -> list[str]:
    """The targets of the rows marked as the current page."""
    return re.findall(r'<a href="([^"]+)" aria-current="page"', nav_section(body))
