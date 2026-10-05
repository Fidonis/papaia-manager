"""The documents folder as the Embedding page browses and selects from it.

What matters is what cannot happen: a path that leaves the root, a link that is followed out
of it, a name the ingester's glob dialect would read as a wildcard, or the staging area of
other administrators showing up in a selection of the whole folder.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.ingest import documents
from app.core.ingest.documents import Documents, clean_rel
from app.core.ingest.errors import InvalidRequest, NotFound


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "documents"
    (base / "handbook" / "chapters").mkdir(parents=True)
    (base / "handbook" / "index.md").write_text("# index", encoding="utf-8")
    (base / "handbook" / "chapters" / "one.md").write_text("one", encoding="utf-8")
    (base / "handbook" / "chapters" / "two.md").write_text("two!", encoding="utf-8")
    (base / "notes.txt").write_text("hello", encoding="utf-8")
    (base / "uploads" / "alice" / "20261005-100000-aaaaaaaa").mkdir(parents=True)
    (base / "uploads" / "alice" / "20261005-100000-aaaaaaaa" / "secret.pdf").write_bytes(b"x")
    return base


@pytest.fixture
def folder(root: Path) -> Documents:
    return Documents(root, "/data/local", hidden_top=frozenset({"uploads"}))


def _link(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating symbolic links")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, ""), ("", ""), ("a/b", "a/b"), ("a//b/", "a/b"), ("/".join(["a"] * 5), "a/a/a/a/a")],
)
def test_a_path_is_reduced_to_plain_segments(raw: str | None, expected: str) -> None:
    assert clean_rel(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["../x", "a/../b", "a/./b", "/etc/passwd", "a\\b", "a/\x00b", "a/\nb", "x" * 300, "a/" * 600],
)
def test_a_path_that_could_leave_the_root_is_refused(raw: str) -> None:
    with pytest.raises(InvalidRequest):
        clean_rel(raw)


def test_resolve_stays_inside_the_root(folder: Documents, root: Path) -> None:
    assert folder.resolve("handbook/index.md") == (root / "handbook" / "index.md").resolve()
    assert folder.resolve("") == root.resolve()


def test_resolve_refuses_a_missing_path_and_the_hidden_staging_area(folder: Documents) -> None:
    with pytest.raises(NotFound):
        folder.resolve("handbook/missing.md")
    with pytest.raises(InvalidRequest):
        folder.resolve("uploads")
    with pytest.raises(InvalidRequest):
        folder.resolve("uploads/alice")


def test_a_symbolic_link_is_not_followed_out_of_the_root(
    folder: Documents, root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.txt").write_text("secret", encoding="utf-8")
    _link(outside, root / "escape")

    with pytest.raises(InvalidRequest, match="Symbolic links"):
        folder.resolve("escape")
    with pytest.raises(InvalidRequest, match="Symbolic links"):
        folder.resolve("escape/stolen.txt")
    assert "escape" not in [entry.name for entry in folder.list_dir("").entries]
    assert all(path != "escape/stolen.txt" for path, _ in folder.files_under(""))


def test_a_linked_file_is_neither_listed_nor_counted(
    folder: Documents, root: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("outside", encoding="utf-8")
    _link(target, root / "handbook" / "alias.txt")

    assert "alias.txt" not in [e.name for e in folder.list_dir("handbook").entries]
    assert "handbook/alias.txt" not in [path for path, _ in folder.files_under("handbook")]


def test_the_ingesters_path_of_a_selection(folder: Documents) -> None:
    assert folder.container_path() == "/data/local"
    assert folder.container_path("handbook//chapters") == "/data/local/handbook/chapters"


# ---------------------------------------------------------------------------
# Listing and counting
# ---------------------------------------------------------------------------


def test_a_listing_has_folders_first_and_hides_the_staging_area(folder: Documents) -> None:
    listing = folder.list_dir("")

    assert [(e.name, e.is_dir) for e in listing.entries] == [
        ("handbook", True),
        ("notes.txt", False),
    ]
    assert listing.entries[1].size == 5
    assert listing.entries[0].rel == "handbook"
    assert not listing.truncated


def test_a_nested_listing_carries_paths_relative_to_the_root(folder: Documents) -> None:
    listing = folder.list_dir("handbook")

    assert [e.rel for e in listing.entries] == ["handbook/chapters", "handbook/index.md"]


def test_a_folder_called_uploads_below_the_top_level_is_an_ordinary_folder(
    root: Path, folder: Documents
) -> None:
    (root / "handbook" / "uploads").mkdir()
    (root / "handbook" / "uploads" / "a.md").write_text("a", encoding="utf-8")

    assert "uploads" in [e.name for e in folder.list_dir("handbook").entries]
    assert "handbook/uploads/a.md" in [p for p, _ in folder.files_under("")]


def test_listing_a_file_is_refused(folder: Documents) -> None:
    with pytest.raises(InvalidRequest, match="not a folder"):
        folder.list_dir("notes.txt")


def test_a_huge_folder_is_cut_off_and_says_so(
    folder: Documents, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(documents, "MAX_LISTING", 3)
    for index in range(10):
        (root / f"f{index}.txt").write_text("x", encoding="utf-8")

    listing = folder.list_dir("")

    assert listing.truncated
    assert len(listing.entries) == 3


def test_files_under_never_reaches_the_staging_area(folder: Documents) -> None:
    paths = sorted(path for path, _ in folder.files_under(""))

    assert paths == [
        "handbook/chapters/one.md",
        "handbook/chapters/two.md",
        "handbook/index.md",
        "notes.txt",
    ]


def test_a_count_counts_each_file_once_however_it_was_selected(folder: Documents) -> None:
    counts = folder.count(["handbook", "handbook/chapters", "handbook/chapters/one.md"])

    assert counts.files == 3
    assert counts.bytes == len("# index") + len("one") + len("two!")


# ---------------------------------------------------------------------------
# Selecting
# ---------------------------------------------------------------------------


def test_a_folder_becomes_a_recursive_glob_and_a_file_its_own_path(folder: Documents) -> None:
    assert folder.include_patterns(["handbook/chapters", "notes.txt"]) == [
        "handbook/chapters/**",
        "notes.txt",
    ]


def test_a_path_under_a_selected_folder_adds_nothing(folder: Documents) -> None:
    patterns = folder.include_patterns(
        ["handbook/chapters/one.md", "handbook", "handbook/chapters"]
    )

    assert patterns == ["handbook/**"]


def test_a_sibling_whose_name_starts_like_a_folder_is_not_covered_by_it(
    root: Path, folder: Documents
) -> None:
    (root / "handbook-old").mkdir()
    (root / "handbook-old" / "a.md").write_text("a", encoding="utf-8")

    assert folder.include_patterns(["handbook", "handbook-old"]) == [
        "handbook/**",
        "handbook-old/**",
    ]


def test_selecting_the_whole_root_needs_no_filter(folder: Documents) -> None:
    assert folder.include_patterns([""]) == []
    assert folder.include_patterns(["handbook", ""]) == []


def test_an_empty_selection_is_refused(folder: Documents) -> None:
    with pytest.raises(InvalidRequest, match="at least one"):
        folder.include_patterns([])


@pytest.mark.parametrize("name", ["what?.md", "star*.md", "dir/a*/b.md"])
def test_a_wildcard_in_a_name_is_refused_because_the_ingester_has_no_escape(
    folder: Documents, name: str
) -> None:
    # Refused before the path is looked up: such a file could not be selected on its own,
    # and some platforms cannot even create one.
    with pytest.raises(InvalidRequest, match="wildcard"):
        folder.include_patterns([name])


def test_a_missing_path_cannot_be_selected(folder: Documents) -> None:
    with pytest.raises(NotFound):
        folder.include_patterns(["nope.md"])


def test_the_staging_area_cannot_be_selected_through_the_folder(folder: Documents) -> None:
    with pytest.raises(InvalidRequest):
        folder.include_patterns(["uploads/alice"])


def test_too_many_separate_selections_are_refused(
    root: Path, folder: Documents, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(documents, "MAX_INCLUDES", 2)
    for index in range(3):
        (root / f"x{index}.txt").write_text("x", encoding="utf-8")

    with pytest.raises(InvalidRequest, match="select their folder"):
        folder.include_patterns(["x0.txt", "x1.txt", "x2.txt"])
