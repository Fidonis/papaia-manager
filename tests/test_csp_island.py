"""Server values for page scripts: JSON data islands (T-UI-03).

`data_island(id, value)` of partials/_csp.html writes a value as
`<script type="application/json">`, which the browser never executes and which
static/js/core/islands.js reads. Whatever the value holds, the island ends where the macro
ends it: `<`, `>`, `&` and `'` are escaped by `tojson`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import design_world  # noqa: E402

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-csp-island-"))
_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

from app.templating import templates  # noqa: E402

HOSTILE = "</script><script>alert(1)</script><!-- <script> & ' \"  "


class _Scripts(HTMLParser):
    """Every script element of a document with its attributes and text, as a browser splits them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self._open: dict[str, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._open, self._text = dict(attrs), []

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._open is not None:
            self.scripts.append((self._open, "".join(self._text)))
            self._open = None


def _render(island_id: str, value: Any) -> str:
    source = (
        '{% from "partials/_csp.html" import data_island %}'
        "<p>a</p>{{ data_island(i, v) }}<p>b</p>"
    )
    return templates.env.from_string(source).render(i=island_id, v=value)


def _islands(markup: str) -> list[tuple[dict[str, str | None], str]]:
    parser = _Scripts()
    parser.feed(markup)
    parser.close()
    return parser.scripts


@pytest.mark.parametrize(
    "value",
    [
        HOSTILE,
        {"name": HOSTILE, "list": [HOSTILE, 1, None, True], "nested": {"k": "<!--"}},
        ["<", ">", "&", "'", "</SCRIPT >", "<!--<script>"],
        42,
        None,
    ],
)
def test_one_island_whose_parsed_value_equals_the_input(value: Any) -> None:
    markup = _render("page-data", value)
    (island,) = _islands(markup)
    attrs, text = island
    assert attrs == {"type": "application/json", "id": "page-data"}
    assert json.loads(text) == value
    assert markup.endswith("<p>b</p>")


def test_the_island_text_carries_no_markup_characters() -> None:
    (island,) = _islands(_render("x", HOSTILE))
    text = island[1]
    for character in "<>&'":
        assert character not in text


@pytest.mark.parametrize("island_id", ["", "a b", 'x"y', "x><script>", "1abc", "a" * 81])
def test_an_island_id_must_be_a_plain_name(island_id: str) -> None:
    with pytest.raises(ValueError, match="island id"):
        _render(island_id, 1)
