"""Only the published documents may sit as Markdown files at the repository root.

The root of the repository is what a visitor sees first, so it holds the project's
public documents and nothing else. Any other Markdown file at the root fails the
build; documentation that belongs to a part of the code lives next to that code.
The check lists the directory itself rather than asking Git, so a file that was
only created and not yet added fails as well.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

ALLOWED_ROOT_MARKDOWN = frozenset({
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_LICENSES.md",
    "TRADEMARK.md",
})

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})


def disallowed_root_markdown(root: Path) -> list[str]:
    """The Markdown files directly in `root` that are not on the allow-list, sorted.

    The suffix is compared without regard to case, so `notes.MD` counts as Markdown;
    the name itself must match the allow-list exactly.
    """
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_file()
        and entry.suffix.lower() in MARKDOWN_SUFFIXES
        and entry.name not in ALLOWED_ROOT_MARKDOWN
    )


def _seed(root: Path, *names: str) -> None:
    for name in names:
        (root / name).write_text("# placeholder\n", encoding="utf-8")


def test_the_repository_root_holds_only_the_allowed_markdown_files() -> None:
    extra = disallowed_root_markdown(ROOT)
    assert extra == [], (
        f"Markdown files at the repository root that are not allowed: {extra}. "
        f"Allowed are {sorted(ALLOWED_ROOT_MARKDOWN)}; put other documentation next to "
        "the code it describes."
    )


def test_the_allowed_files_pass(tmp_path: Path) -> None:
    _seed(tmp_path, *ALLOWED_ROOT_MARKDOWN)
    assert disallowed_root_markdown(tmp_path) == []


@pytest.mark.parametrize("name", ["NOTES.md", "notes.MD", "Guide.markdown", "SECURITY.markdown"])
def test_a_seeded_extra_markdown_file_fails(tmp_path: Path, name: str) -> None:
    _seed(tmp_path, *ALLOWED_ROOT_MARKDOWN, name)
    assert disallowed_root_markdown(tmp_path) == [name]


def test_markdown_below_the_root_and_other_root_files_are_not_checked(tmp_path: Path) -> None:
    _seed(tmp_path, "README.md", "LICENSE", "ruff.toml")
    (tmp_path / "docs").mkdir()
    _seed(tmp_path / "docs", "guide.md")
    (tmp_path / "folder.md").mkdir()
    assert disallowed_root_markdown(tmp_path) == []
