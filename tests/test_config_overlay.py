"""The overlay files and their history: what is written where, who wins a race, and what is kept.

No Docker and no core: the store works on a configuration directory in `tmp_path`.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.config import history, overlay
from app.core.config.overlay import OverlayError

KEY = "librechat"


@pytest.fixture
def config(tmp_path: Path) -> str:
    return str(tmp_path / "config")


def _file(config: str, key: str = KEY) -> Path:
    return overlay.path_of(config, key)


# -- targets ------------------------------------------------------------------------------


def test_the_targets_are_keys_and_the_paths_are_the_cores() -> None:
    assert {t.key: t.path for t in overlay.TARGETS} == {
        "librechat": "ai/librechat/librechat.yaml",
        "litellm": "ai/litellm/config.yaml",
        "searxng": "services/searxng/settings.yml",
    }


@pytest.mark.parametrize(
    "key", ["", "../etc", "ai/librechat/librechat.yaml", "keycloak", "LIBRECHAT"]
)
def test_a_path_is_not_a_target(key: str) -> None:
    with pytest.raises(OverlayError, match="Unknown"):
        overlay.target(key)
    with pytest.raises(OverlayError):
        overlay.path_of("/c", key)


def test_every_core_target_is_either_offered_or_listed_as_not_editable() -> None:
    offered = {t.path for t in overlay.TARGETS}
    assert offered.isdisjoint(overlay.OTHER_RENDER_TARGETS)


# -- reading and the revision ----------------------------------------------------------------


def test_a_missing_file_has_an_empty_revision(config: str) -> None:
    state = overlay.read(config, KEY)
    assert (state.exists, state.revision, state.text, state.lines) == (False, "", "", 0)


def test_an_empty_file_is_not_a_missing_one(config: str) -> None:
    _file(config).parent.mkdir(parents=True)
    _file(config).write_bytes(b"")
    state = overlay.read(config, KEY)
    assert state.exists
    assert state.revision != ""
    assert state.revision == overlay.revision_of(b"")


def test_a_symbolic_link_is_refused(config: str, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text("a: 1\n", encoding="utf-8")
    _file(config).parent.mkdir(parents=True)
    try:
        _file(config).symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    with pytest.raises(OverlayError, match="symbolic link"):
        overlay.read(config, KEY)


# -- compare-and-swap -------------------------------------------------------------------


def test_a_write_creates_the_file_and_its_directories(config: str) -> None:
    written = overlay.write(config, KEY, b"a: 1\n", expected_revision="")
    assert written is not None
    assert written.text == "a: 1\n"
    assert _file(config).read_bytes() == b"a: 1\n"
    assert not list(_file(config).parent.glob(".*manager-tmp"))


def test_a_write_from_a_stale_page_changes_nothing(config: str) -> None:
    first = overlay.write(config, KEY, b"a: 1\n", expected_revision="")
    assert first is not None
    _file(config).write_bytes(b"a: 2  # edited on the host\n")
    assert overlay.write(config, KEY, b"a: 3\n", expected_revision=first.revision) is None
    assert _file(config).read_bytes() == b"a: 2  # edited on the host\n"


def test_a_write_over_a_file_created_meanwhile_is_refused(config: str) -> None:
    _file(config).parent.mkdir(parents=True)
    _file(config).write_bytes(b"host: true\n")
    assert overlay.write(config, KEY, b"a: 1\n", expected_revision="") is None
    assert _file(config).read_bytes() == b"host: true\n"


def test_a_replacement_keeps_the_mode_of_the_file(config: str) -> None:
    if os.name == "nt":
        pytest.skip("modes are not meaningful here")
    first = overlay.write(config, KEY, b"a: 1\n", expected_revision="")
    assert first is not None
    _file(config).chmod(0o640)
    second = overlay.write(config, KEY, b"a: 2\n", expected_revision=first.revision)
    assert second is not None
    assert (_file(config).stat().st_mode & 0o777) == 0o640


def test_a_new_file_is_readable_by_its_owner_only(config: str) -> None:
    if os.name == "nt":
        pytest.skip("modes are not meaningful here")
    overlay.write(config, KEY, b"a: 1\n", expected_revision="")
    assert (_file(config).stat().st_mode & 0o777) == 0o600


def test_two_writers_with_the_same_revision_cannot_both_win(config: str) -> None:
    base = overlay.write(config, KEY, b"a: 0\n", expected_revision="")
    assert base is not None
    results: list[object] = []

    def attempt(text: bytes) -> None:
        results.append(overlay.write(config, KEY, text, expected_revision=base.revision))

    threads = [threading.Thread(target=attempt, args=(f"a: {i}\n".encode(),)) for i in range(1, 6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r is not None) == 1


def test_delete_is_compare_and_swap_too(config: str) -> None:
    first = overlay.write(config, KEY, b"a: 1\n", expected_revision="")
    assert first is not None
    assert overlay.delete(config, KEY, expected_revision="stale") is False
    assert _file(config).exists()
    assert overlay.delete(config, KEY, expected_revision=first.revision) is True
    assert not _file(config).exists()


# -- the check --------------------------------------------------------------------------------


def test_a_draft_that_parses_has_no_problems() -> None:
    assert overlay.check("") == []
    assert overlay.check("# only a comment\n") == []
    assert overlay.check("a:\n  - b\n  - c: 1\n") == []


def test_a_syntax_error_names_the_line() -> None:
    problems = overlay.check("a: 1\nb: [\n")
    assert len(problems) == 1
    assert problems[0].line is not None and problems[0].line >= 2


def test_a_tab_for_indentation_is_a_problem() -> None:
    assert overlay.check("a:\n\tb: 1\n")


@pytest.mark.parametrize("text", ["- a\n- b\n", "just text\n", "42\n"])
def test_the_top_level_must_be_a_mapping(text: str) -> None:
    problems = overlay.check(text)
    assert [p.message for p in problems] == ["The top level must be a mapping (key: value)."]


def test_a_draft_over_the_size_limit_is_refused() -> None:
    assert "larger" in overlay.check("a: " + "x" * overlay.MAX_BYTES)[0].message


def test_a_nul_character_is_refused() -> None:
    assert overlay.check("a: 1\x00\n")


def test_the_summary_counts_the_top_level_keys() -> None:
    assert overlay.summary("a: 1\nb: 2\n") == "top level is a mapping with 2 keys"
    assert overlay.summary("a: 1\n") == "top level is a mapping with 1 key"
    assert "changes nothing" in overlay.summary("# nothing\n")


# -- other files ----------------------------------------------------------------------------


def test_files_the_editor_does_not_offer_are_listed_with_the_reason(config: str) -> None:
    base = Path(config) / "overlay"
    for rel in (
        "ai/librechat/librechat.yaml",
        "ai/litellm/prometheus.yml",
        "infra/keycloak/keycloak.conf",
        "ai/localai/models/extra.yaml",
        "ai/librechat/typo.yaml",
        "ai/.hidden",
    ):
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text("x: 1\n", encoding="utf-8")
    found = {item["path"]: item["note"] for item in overlay.other_files(config)}
    assert found == {
        "ai/litellm/prometheus.yml": "used by the render, not editable here",
        "infra/keycloak/keycloak.conf": "used by the render, not editable here",
        "ai/localai/models/extra.yaml": "used by the render, not editable here",
        "ai/librechat/typo.yaml": "ignored by the render",
    }


def test_no_overlay_directory_is_no_files(config: str) -> None:
    assert overlay.other_files(config) == []


# -- history -----------------------------------------------------------------------------------


def test_a_saved_version_is_the_old_bytes(config: str) -> None:
    now = datetime(2026, 10, 18, 14, 25, 30, tzinfo=UTC)
    version_id = history.save(config, KEY, b"a: 1\n", "apply", now=now)
    assert version_id == "20261018T142530Z-apply"
    assert history.read(config, KEY, version_id) == b"a: 1\n"
    [version] = history.versions(config, KEY)
    assert (version.id, version.event, version.size) == (version_id, "apply", 5)
    assert version.when == now
    assert version.revision == overlay.revision_of(b"a: 1\n")


def test_two_saves_in_the_same_second_both_survive(config: str) -> None:
    now = datetime(2026, 10, 18, 14, 25, 30, tzinfo=UTC)
    one = history.save(config, KEY, b"1", "apply", now=now)
    two = history.save(config, KEY, b"2", "apply", now=now)
    assert one != two
    assert history.read(config, KEY, one) == b"1"
    assert history.read(config, KEY, two) == b"2"
    assert [v.id for v in history.versions(config, KEY)] == [two, one]


def test_versions_come_newest_first(config: str) -> None:
    for minute in (1, 3, 2):
        history.save(
            config, KEY, b"x", "apply", now=datetime(2026, 10, 18, 14, minute, 0, tzinfo=UTC)
        )
    assert [v.when.minute for v in history.versions(config, KEY)] == [3, 2, 1]


def test_only_the_latest_are_kept(config: str) -> None:
    for i in range(history.KEEP + 5):
        history.save(
            config,
            KEY,
            str(i).encode(),
            "apply",
            now=datetime(2026, 10, 18, 10, 0, 0, tzinfo=UTC).replace(
                minute=i % 60, hour=10 + i // 60
            ),
        )
    found = history.versions(config, KEY)
    assert len(found) == history.KEEP
    # The oldest five are the ones that went.
    assert history.read(config, KEY, found[-1].id) == b"5"


def test_the_history_of_one_target_is_not_the_history_of_another(config: str) -> None:
    history.save(config, "librechat", b"a", "apply")
    assert history.versions(config, "litellm") == []


@pytest.mark.parametrize(
    "version_id",
    [
        "../../x",
        "20261018T142530Z-apply/../..",
        "x",
        "",
        "20261018T142530Z-evil",
        "20261018T142530Z-apply.bak",
    ],
)
def test_a_version_id_that_is_not_one_of_ours_never_reaches_a_path(
    config: str, version_id: str
) -> None:
    assert history.valid_id(version_id) is False
    with pytest.raises(OverlayError, match="Unknown version"):
        history.read(config, KEY, version_id)


def test_an_unknown_version_is_unknown(config: str) -> None:
    with pytest.raises(OverlayError, match="Unknown version"):
        history.read(config, KEY, "20261018T142530Z-apply")


def test_the_history_is_for_its_owner_only(config: str) -> None:
    if os.name == "nt":
        pytest.skip("modes are not meaningful here")
    version_id = history.save(config, KEY, b"secret: 1\n", "apply")
    directory = history.history_dir(config, KEY)
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / f"{version_id}.bak").stat().st_mode & 0o777) == 0o600


def test_an_event_that_is_not_one_of_ours_is_a_bug(config: str) -> None:
    with pytest.raises(ValueError, match="unknown event"):
        history.save(config, KEY, b"x", "delete")


def test_a_history_that_cannot_be_written_stops_the_change(
    config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object, **__: object) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(history.os, "open", broken)
    with pytest.raises(OverlayError, match="nothing was changed"):
        history.save(config, KEY, b"x", "apply")
