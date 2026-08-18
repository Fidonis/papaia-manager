"""The brand layer is vendored into `Fidonis/qdrant-ingest`.

This repository is the source of that copy. There is no package and no
shared build: the files are copied over there, each carrying a stamp. These
tests catch the two ways the *source* side of that contract rots -- a file
that lost its stamp, and stamps that disagree with each other.

What they cannot catch is drift against the other repository. That is a
release-checklist rule, not a check: a brand change is finished when both
interfaces carry it in the same revision. See CONTRIBUTING.md and
Fidonis/qdrant-ingest's docs/ui.md.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Every file carrying part of the vendored brand layer.
BRAND_FILES = (
    "docker/tailwind.config.js",
    "docker/tailwind.brand.css",
    "docker/tailwind.app.css",
    "src/app/templating.py",
)

_STAMP_RE = re.compile(r"fidonis-brand:\s*(\d+)")

# The <head> block, the brand mark and the theme toggle are vendored inside
# base.html rather than in a file of their own, so the stamp lives in a Jinja
# comment there -- see that comment for exactly which parts of the template
# it covers.
STAMPED_TEMPLATES = ("src/app/templates/base.html",)


def _stamp(relative: str) -> int:
    text = (REPO / relative).read_text(encoding="utf-8")
    match = _STAMP_RE.search(text)
    assert match is not None, f"{relative} carries no fidonis-brand stamp"
    return int(match.group(1))


def test_every_brand_file_is_stamped() -> None:
    for relative in BRAND_FILES + STAMPED_TEMPLATES:
        assert (REPO / relative).is_file(), f"{relative} is missing"
        _stamp(relative)


def test_all_brand_stamps_agree() -> None:
    """A partial edit is the failure this is here to catch."""
    stamps = {relative: _stamp(relative) for relative in BRAND_FILES + STAMPED_TEMPLATES}
    assert len(set(stamps.values())) == 1, f"brand stamps disagree: {stamps}"


def test_the_theme_definitions_are_present() -> None:
    config = (REPO / "docker/tailwind.config.js").read_text(encoding="utf-8")
    assert "fidonis-light" in config
    assert "fidonis-dark" in config


def test_font_urls_stay_relative() -> None:
    """Absolute /static paths are what qdrant-ingest's copy deliberately
    avoids, since it is served from a mounted sub-application. Keeping this
    repo's own URLs relative too is what makes the two files byte-identical.
    """
    brand = (REPO / "docker/tailwind.brand.css").read_text(encoding="utf-8")
    assert 'url("fonts/files/' in brand
    assert 'url("/static/' not in brand


def test_the_stylesheet_inputs_stay_separate() -> None:
    """The brand half must remain diffable against the vendored copy.

    Tailwind takes one input, so the Dockerfile concatenates the two. If they
    were ever merged into one file at rest, the byte comparison that keeps
    the two repositories aligned would have nothing to compare.
    """
    dockerfile = (REPO / "docker/Dockerfile").read_text(encoding="utf-8")
    assert "cat tailwind.brand.css tailwind.app.css > tailwind.input.css" in dockerfile
    assert not (REPO / "docker/tailwind.input.css").exists()
