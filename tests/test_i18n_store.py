"""The per-account preferences file: what an account chose, kept on the server by its subject."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

from app.core import user_prefs

SUBJECT = "6f1c2b1e-0b5e-4c34-9d0d-0d9b2f0a7a11"


@pytest.fixture
def config_dir(tmp_path: Path) -> str:
    return str(tmp_path / "config")


def test_nothing_is_stored_before_an_account_chooses(config_dir: str) -> None:
    assert user_prefs.stored_locale(config_dir, SUBJECT) is None


def test_a_choice_is_remembered_per_account(config_dir: str) -> None:
    user_prefs.set_locale(config_dir, SUBJECT, "de")
    user_prefs.set_locale(config_dir, "someone-else", "en")

    assert user_prefs.stored_locale(config_dir, SUBJECT) == "de"
    assert user_prefs.stored_locale(config_dir, "someone-else") == "en"
    assert user_prefs.stored_locale(config_dir, "a-third-one") is None


def test_automatic_removes_the_entry_instead_of_storing_a_value(config_dir: str) -> None:
    user_prefs.set_locale(config_dir, SUBJECT, "de")
    user_prefs.set_locale(config_dir, SUBJECT, None)

    assert user_prefs.stored_locale(config_dir, SUBJECT) is None
    document = yaml.safe_load(user_prefs.preferences_path(config_dir).read_text("utf-8"))
    assert SUBJECT not in document["users"]


def test_the_file_is_owned_by_the_manager_directory_and_readable_by_the_owner_only(
    config_dir: str,
) -> None:
    user_prefs.set_locale(config_dir, SUBJECT, "de")

    path = user_prefs.preferences_path(config_dir)
    assert path == Path(config_dir) / "manager" / "preferences.yaml"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not path.with_name(path.name + ".tmp").exists()


def test_a_change_made_by_hand_is_picked_up_without_a_restart(config_dir: str) -> None:
    user_prefs.set_locale(config_dir, SUBJECT, "de")
    assert user_prefs.stored_locale(config_dir, SUBJECT) == "de"

    path = user_prefs.preferences_path(config_dir)
    path.write_text(f"users:\n  {SUBJECT}:\n    locale: en-gb-extra-long\n", encoding="utf-8")

    assert user_prefs.stored_locale(config_dir, SUBJECT) == "en-gb-extra-long"


def test_a_damaged_file_reads_as_no_preference_and_is_never_overwritten(
    config_dir: str,
) -> None:
    path = user_prefs.preferences_path(config_dir)
    path.parent.mkdir(parents=True)
    path.write_text("users: [this is: not valid", encoding="utf-8")

    assert user_prefs.stored_locale(config_dir, SUBJECT) is None
    with pytest.raises(user_prefs.PreferencesError):
        user_prefs.set_locale(config_dir, SUBJECT, "de")
    assert path.read_text(encoding="utf-8") == "users: [this is: not valid"


def test_unknown_keys_are_ignored_and_a_numeric_subject_is_a_string(config_dir: str) -> None:
    path = user_prefs.preferences_path(config_dir)
    path.parent.mkdir(parents=True)
    path.write_text(
        "future: 1\nusers:\n  12345:\n    locale: de\n    theme: dark\n", encoding="utf-8"
    )

    assert user_prefs.stored_locale(config_dir, "12345") == "de"


@pytest.mark.parametrize("subject", ["", "x" * 513, "a\nb", "a\x00b"])
def test_an_unusable_subject_is_refused(config_dir: str, subject: str) -> None:
    assert user_prefs.stored_locale(config_dir, subject) is None
    with pytest.raises(user_prefs.PreferencesError):
        user_prefs.set_locale(config_dir, subject, "de")
