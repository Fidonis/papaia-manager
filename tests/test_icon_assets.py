"""The icon files shipped under `src/app/static/icons/` and their attribution.

Every file is somebody's logo or graphic, so a file without a documented source
and licence is a defect. These tests keep `NOTICE.txt` in step with the
directory, and keep the files themselves to plain vector graphics: they are
shown through `<img>`, where a script would not run, but a file that could
carry one or point at another host has no place in a bundled set.
"""

import re
from pathlib import Path

import pytest

ICONS = Path(__file__).resolve().parents[1] / "src" / "app" / "static" / "icons"

# A file name alone at the start of a line is an entry in NOTICE.txt.
_ENTRY_RE = re.compile(r"^(\S+\.svg)\s*$", re.MULTILINE)

_FORBIDDEN_RE = re.compile(
    r"<script|<image|<foreignObject|<iframe|<!DOCTYPE|<!ENTITY|\son\w+\s*=",
    re.IGNORECASE,
)
_HREF_RE = re.compile(r"""(?:xlink:)?href\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_URL_RE = re.compile(r"url\(\s*([^)]*)\)", re.IGNORECASE)


def _svg_files() -> list[Path]:
    return sorted(ICONS.glob("*.svg"))


def _notice_entries() -> set[str]:
    return set(_ENTRY_RE.findall((ICONS / "NOTICE.txt").read_text(encoding="utf-8")))


def test_there_are_icons_to_check() -> None:
    assert _svg_files()


def test_every_icon_file_has_a_notice_entry() -> None:
    missing = {path.name for path in _svg_files()} - _notice_entries()
    assert not missing, f"no entry in NOTICE.txt for: {sorted(missing)}"


def test_every_notice_entry_names_a_file_that_exists() -> None:
    stale = {name for name in _notice_entries() if not (ICONS / name).is_file()}
    assert not stale, f"NOTICE.txt lists files that are not there: {sorted(stale)}"


def test_the_licence_texts_the_notice_relies_on_are_shipped() -> None:
    notice = (ICONS / "NOTICE.txt").read_text(encoding="utf-8")

    assert "LICENSE-dashboard-icons.txt" in notice
    text = (ICONS / "LICENSE-dashboard-icons.txt").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0" in text
    # CC BY 4.0 is credited and linked in the notice instead of copied: the licence asks
    # for the licence text "or a link to it".
    assert "https://creativecommons.org/licenses/by/4.0/" in notice


@pytest.mark.parametrize("path", _svg_files(), ids=lambda path: path.name)
def test_an_icon_is_a_plain_self_contained_vector_graphic(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert text.lstrip().startswith("<svg"), "not an SVG document"
    assert not _FORBIDDEN_RE.search(text)
    external = [ref for ref in _HREF_RE.findall(text) if not ref.startswith("#")]
    assert not external, f"references outside the file: {external}"
    external = [ref for ref in _URL_RE.findall(text) if not ref.strip("'\" ").startswith("#")]
    assert not external, f"url() outside the file: {external}"


@pytest.mark.parametrize("path", _svg_files(), ids=lambda path: path.name)
def test_an_icon_stays_small(path: Path) -> None:
    """The dashboard draws it at 44 px; anything large is a photograph or a bundle."""
    assert path.stat().st_size < 64 * 1024
