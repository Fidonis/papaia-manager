"""Checks of a rendered page under the enforced policy (T-UI-03), for page tests.

Every page carries the strict marker, loads the CSP build of Alpine and no other, has no
executable inline script and no inline event handler, and loads its page module
(static/js/pages/<group>.js) as an ES module after the shared main.js and before Alpine, so
the module registers its components before Alpine starts. Server values reach the module only
through data islands, read back here as JSON. A test tool, never part of the image.
"""

from __future__ import annotations

import json
import re
from typing import Any

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_ON_ATTRIBUTE = re.compile(r"<[a-zA-Z][^<>]*\son[a-z]+\s*=", re.DOTALL)
_DATA_TYPES = ('type="application/json"',)


def _scripts(html: str) -> list[tuple[str, str]]:
    return [(attrs, body) for attrs, body in _SCRIPT.findall(html)]


def assert_converted_page(html: str, module: str) -> None:
    """`html` is a page under the policy that loads `module` (a path under static/js/pages/)."""
    assert html.count('<meta name="papaia-csp" content="strict">') == 1
    scripts = _scripts(html)
    inline = [
        body
        for attrs, body in scripts
        if "src=" not in attrs and not any(kind in attrs for kind in _DATA_TYPES)
    ]
    assert inline == [], inline
    assert _ON_ATTRIBUTE.search(html) is None
    sources = [re.search(r'src="([^"]+)"', attrs) for attrs, _ in scripts]
    order = [m.group(1).split("?", 1)[0] for m in sources if m]
    assert "/static/alpine-csp.min.js" in order
    assert "/static/alpine.min.js" not in order
    page = f"/static/js/pages/{module}"
    assert order.count(page) == 1, order
    assert order.index("/static/js/core/main.js") < order.index(page)
    assert order.index(page) < order.index("/static/alpine-csp.min.js")
    tag = next(attrs for attrs, _ in scripts if f'src="{page}' in attrs)
    assert 'type="module"' in tag


def island(html: str, island_id: str) -> Any:
    """The value of the data island `island_id` of `html`."""
    found = [
        body
        for attrs, body in _scripts(html)
        if re.search(rf'\bid="{re.escape(island_id)}"', attrs)
        and 'type="application/json"' in attrs
    ]
    assert len(found) == 1, f"{island_id}: {len(found)} islands"
    return json.loads(found[0])
