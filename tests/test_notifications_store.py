"""The recipients and what is stored about them: the rules, the file and its revision.

Pure logic and a temporary directory, no manager. What is asserted is what the page relies on:
that a refusal is a sentence which repeats nothing it was given, that a stale change writes
nothing, that a file nobody can read is shown and never replaced, and that the file which holds
webhook URLs is private.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from app.core.notifications import store
from app.core.notifications.model import (
    CATALOGUE,
    MAX_RECIPIENTS,
    Event,
    EventSettings,
    Kind,
    NotificationsError,
    mask_url,
    validate_events,
    validate_name,
    validate_target,
    validate_webhook_url,
)

_HOOK = "https://hooks.example.com/services/T000/B000/s3cr3t-token"


def _add(config_dir: Path, revision: str, **overrides: object) -> tuple[store.ConfigLoad, object]:
    arguments: dict[str, object] = {
        "name": "Ops team",
        "kind": Kind.EMAIL,
        "target": "ops@customer.example",
        "enabled": True,
        "events": [Event.JOB_FAILED],
    }
    arguments.update(overrides)
    return store.add_recipient(str(config_dir), revision, **arguments)  # type: ignore[arg-type]


# -- the rules -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "ftp://hooks.example.com/x",
        "hooks.example.com/x",
        "https://",
        "https://user:pw@hooks.example.com/x",
        "https://user@hooks.example.com/x",
        "https://hooks.example.com/with space",
        "https://hooks.example.com:notaport/x",
        "https://hooks.example.com/" + "a" * 2100,
    ],
)
def test_a_webhook_url_that_is_not_usable_is_refused_without_repeating_it(value: str) -> None:
    with pytest.raises(NotificationsError) as raised:
        validate_webhook_url(value)
    message = str(raised.value)
    assert message
    assert value.strip() == "" or value.strip() not in message


@pytest.mark.parametrize(
    "value",
    [
        "https://hooks.example.com/x",
        "http://n8n:5678/webhook/abc",
        "https://hooks.example.com:8443/x?token=1",
        "http://10.0.0.5/hook",
        "https://[::1]:8080/hook",
    ],
)
def test_an_http_or_https_url_with_a_host_is_accepted_as_it_is(value: str) -> None:
    assert validate_webhook_url(value) == value


@pytest.mark.parametrize("value", ["", "ops", "ops@", "@example.org", "a b@example.org", "x@y"])
def test_an_email_address_that_is_not_one_is_refused(value: str) -> None:
    with pytest.raises(NotificationsError):
        validate_target(Kind.EMAIL, value)


def test_a_name_is_trimmed_collapsed_and_limited() -> None:
    assert validate_name("  Ops   team ") == "Ops team"
    with pytest.raises(NotificationsError):
        validate_name("   ")
    with pytest.raises(NotificationsError):
        validate_name("x" * 61)


def test_events_are_kept_once_and_in_the_order_of_the_columns() -> None:
    shuffled = [Event.CERT_EXPIRING, Event.JOB_FAILED, Event.CERT_EXPIRING, Event.BACKUP_OVERDUE]
    assert validate_events(shuffled) == [
        Event.JOB_FAILED,
        Event.BACKUP_OVERDUE,
        Event.CERT_EXPIRING,
    ]
    assert [info.event for info in CATALOGUE] == list(Event)


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (_HOOK, "https://hooks.example.com/…"),
        ("https://hooks.example.com", "https://hooks.example.com"),
        ("https://hooks.example.com/", "https://hooks.example.com"),
        ("http://n8n:5678/webhook/abc", "http://n8n:5678/…"),
        ("https://hooks.example.com/?key=secret", "https://hooks.example.com/…"),
        ("https://[::1]:8080/hook", "https://[::1]:8080/…"),
    ],
)
def test_a_webhook_is_shown_by_where_it_goes_and_never_by_what_authorises_it(
    url: str, shown: str
) -> None:
    assert mask_url(url) == shown
    assert "s3cr3t" not in mask_url(_HOOK)


def test_the_settings_have_the_documented_defaults_and_limits() -> None:
    defaults = EventSettings()
    assert (
        defaults.check_interval_minutes,
        defaults.debounce_minutes,
        defaults.cert_lead_days,
    ) == (
        5,
        30,
        14,
    )
    for field, bad in (
        ("check_interval_minutes", 0),
        ("check_interval_minutes", 61),
        ("debounce_minutes", 0),
        ("debounce_minutes", 1441),
        ("cert_lead_days", 0),
        ("cert_lead_days", 91),
    ):
        with pytest.raises(ValueError):  # noqa: PT011 -- pydantic's ValidationError is one
            EventSettings(**{field: bad})


# -- the file ------------------------------------------------------------------------


def test_without_a_file_there_is_an_empty_configuration_and_no_revision(tmp_path: Path) -> None:
    load = store.load_config(str(tmp_path))
    assert load.error is None
    assert load.revision == ""
    assert load.config.recipients == []
    assert load.config.settings == EventSettings()


def test_a_recipient_is_stored_and_read_back(tmp_path: Path) -> None:
    loaded, created = _add(tmp_path, "", events=[Event.JOB_FAILED, Event.BACKUP_OVERDUE])
    again = store.load_config(str(tmp_path))
    assert again.error is None
    assert again.revision == loaded.revision != ""
    (recipient,) = again.config.recipients
    assert recipient == created
    assert recipient.name == "Ops team"
    assert recipient.events == [Event.JOB_FAILED, Event.BACKUP_OVERDUE]
    assert len(recipient.id) == 8


@pytest.mark.skipif(os.name == "nt", reason="file modes are not a thing on Windows")
def test_the_file_that_holds_webhook_urls_is_private(tmp_path: Path) -> None:
    _add(tmp_path, "", name="Chat", kind=Kind.WEBHOOK, target=_HOOK)
    mode = stat.S_IMODE(store.config_path(str(tmp_path)).stat().st_mode)
    assert mode == 0o600


def test_nothing_is_left_behind_next_to_the_file(tmp_path: Path) -> None:
    _add(tmp_path, "")
    assert sorted(p.name for p in store.config_path(str(tmp_path)).parent.iterdir()) == [
        store.CONFIG_NAME
    ]


def test_a_change_made_on_a_stale_revision_writes_nothing(tmp_path: Path) -> None:
    first, _ = _add(tmp_path, "")
    before = store.config_path(str(tmp_path)).read_bytes()
    with pytest.raises(store.StaleRevisionError):
        _add(tmp_path, "not-the-current-revision", name="Second", target="two@customer.example")
    assert store.config_path(str(tmp_path)).read_bytes() == before
    # ... and the one that holds the current revision goes through.
    second, _ = _add(tmp_path, first.revision, name="Second", target="two@customer.example")
    assert [r.name for r in second.config.recipients] == ["Ops team", "Second"]


def test_a_creation_on_a_stale_empty_revision_is_refused_too(tmp_path: Path) -> None:
    _add(tmp_path, "")
    with pytest.raises(store.StaleRevisionError):
        _add(tmp_path, "", name="Other", target="other@customer.example")


def test_two_recipients_may_not_share_a_name_whatever_its_case(tmp_path: Path) -> None:
    first, _ = _add(tmp_path, "")
    with pytest.raises(NotificationsError) as raised:
        _add(tmp_path, first.revision, name="ops TEAM", target="other@customer.example")
    assert "same name" in str(raised.value)
    assert len(store.load_config(str(tmp_path)).config.recipients) == 1


def test_there_is_a_limit_to_the_number_of_recipients(tmp_path: Path) -> None:
    revision = ""
    for index in range(MAX_RECIPIENTS):
        loaded, _ = _add(tmp_path, revision, name=f"r{index}", target=f"r{index}@customer.example")
        revision = loaded.revision
    with pytest.raises(NotificationsError):
        _add(tmp_path, revision, name="one too many", target="more@customer.example")


def test_a_refused_value_changes_nothing(tmp_path: Path) -> None:
    first, _ = _add(tmp_path, "")
    before = store.config_path(str(tmp_path)).read_bytes()
    with pytest.raises(NotificationsError):
        _add(tmp_path, first.revision, name="Bad", target="not an address")
    assert store.config_path(str(tmp_path)).read_bytes() == before


def test_changing_a_recipient_without_a_target_keeps_the_stored_one(tmp_path: Path) -> None:
    first, created = _add(tmp_path, "", name="Chat", kind=Kind.WEBHOOK, target=_HOOK)
    loaded, changed = store.update_recipient(
        str(tmp_path),
        first.revision,
        created.id,  # type: ignore[attr-defined]
        name="On-call chat",
        events=[Event.SERVICE_UNHEALTHY, Event.JOB_FAILED],
        enabled=False,
    )
    assert changed.target == _HOOK
    assert changed.name == "On-call chat"
    assert changed.enabled is False
    assert changed.events == [Event.JOB_FAILED, Event.SERVICE_UNHEALTHY]
    assert store.load_config(str(tmp_path)).config.recipients[0].target == _HOOK
    assert loaded.revision != first.revision


def test_a_new_target_replaces_the_stored_one_and_is_checked(tmp_path: Path) -> None:
    first, created = _add(tmp_path, "", name="Chat", kind=Kind.WEBHOOK, target=_HOOK)
    rid = created.id  # type: ignore[attr-defined]
    with pytest.raises(NotificationsError):
        store.update_recipient(str(tmp_path), first.revision, rid, target="ftp://nope")
    _, changed = store.update_recipient(
        str(tmp_path), first.revision, rid, target="https://other.example.com/hook"
    )
    assert changed.target == "https://other.example.com/hook"


def test_an_unknown_recipient_is_a_lookup_error(tmp_path: Path) -> None:
    first, _ = _add(tmp_path, "")
    with pytest.raises(LookupError):
        store.update_recipient(str(tmp_path), first.revision, "nope", name="x")
    with pytest.raises(LookupError):
        store.delete_recipient(str(tmp_path), first.revision, "nope")


def test_deleting_a_recipient_takes_its_subscriptions_and_its_test_result_with_it(
    tmp_path: Path,
) -> None:
    first, created = _add(tmp_path, "", events=list(Event))
    rid = created.id  # type: ignore[attr-defined]
    store.update_state(
        str(tmp_path),
        lambda state: state.tests.__setitem__(
            rid, store.TestResult("2026-10-12T14:00:00+00:00", True)
        ),
    )
    loaded, name = store.delete_recipient(str(tmp_path), first.revision, rid)
    assert name == "Ops team"
    assert loaded.config.recipients == []
    assert rid not in store.load_state(str(tmp_path)).tests
    assert (
        yaml.safe_load(store.config_path(str(tmp_path)).read_text(encoding="utf-8"))["recipients"]
        == []
    )


def test_the_settings_are_stored_with_the_recipients_left_alone(tmp_path: Path) -> None:
    first, _ = _add(tmp_path, "")
    loaded = store.save_settings(
        str(tmp_path),
        first.revision,
        EventSettings(check_interval_minutes=10, debounce_minutes=60, cert_lead_days=21),
    )
    again = store.load_config(str(tmp_path)).config
    assert again.settings.check_interval_minutes == 10
    assert again.settings.cert_lead_days == 21
    assert len(again.recipients) == 1
    assert loaded.revision != first.revision


# -- a file nobody can use -----------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        ": : not yaml : [",
        "recipients: nope",
        "recipients:\n  - {id: a, name: x, kind: email, target: not-an-address}\n",
        "surprise: 1\n",
        "recipients:\n"
        "  - {id: a, name: Same, kind: email, target: a@customer.example}\n"
        "  - {id: b, name: same, kind: email, target: b@customer.example}\n",
    ],
)
def test_a_damaged_file_is_reported_and_never_replaced(tmp_path: Path, content: str) -> None:
    path = store.config_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    load = store.load_config(str(tmp_path))
    assert load.error
    assert load.config.recipients == []
    assert load.revision != ""
    with pytest.raises(store.ConfigUnreadableError):
        _add(tmp_path, load.revision)
    assert path.read_text(encoding="utf-8") == content


def test_the_report_of_a_damaged_file_repeats_none_of_its_values(tmp_path: Path) -> None:
    path = store.config_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text(
        "recipients:\n  - {id: a, name: Chat, kind: webhook, target: ftp://s3cr3t.example/x}\n",
        encoding="utf-8",
    )
    assert "s3cr3t" not in (store.load_config(str(tmp_path)).error or "")


# -- the state -----------------------------------------------------------------------


def test_the_state_is_stored_and_read_back(tmp_path: Path) -> None:
    def remember(state: store.NotifyState) -> str:
        state.seen["backup_overdue|backup"] = store.Seen(active=True, marker="m", last_sent="t")
        state.tests["abc"] = store.TestResult("2026-10-12T14:00:00+00:00", False, "no route")
        return "done"

    assert store.update_state(str(tmp_path), remember) == "done"
    state = store.load_state(str(tmp_path))
    assert state.seen["backup_overdue|backup"] == store.Seen(True, "m", "t")
    assert state.tests["abc"] == store.TestResult("2026-10-12T14:00:00+00:00", False, "no route")


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"seen": 3}', '{"tests": {"x": 1}}'])
def test_a_state_that_cannot_be_read_is_forgotten_not_raised(tmp_path: Path, content: str) -> None:
    path = store.state_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    state = store.load_state(str(tmp_path))
    assert state.seen == {} and state.tests == {}


def test_the_services_seen_running_are_stored_sorted_and_read_back(tmp_path: Path) -> None:
    def remember(state: store.NotifyState) -> None:
        state.up = {"n8n/n8n", "keycloak", "localai"}

    store.update_state(str(tmp_path), remember)
    raw = json.loads(store.state_path(str(tmp_path)).read_text(encoding="utf-8"))
    assert raw["up"] == ["keycloak", "localai", "n8n/n8n"]
    assert store.load_state(str(tmp_path)).up == {"n8n/n8n", "keycloak", "localai"}


def test_a_state_written_before_the_services_were_remembered_has_none(tmp_path: Path) -> None:
    path = store.state_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "seen": {}, "tests": {}}', encoding="utf-8")
    assert store.load_state(str(tmp_path)).up == set()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"up": "keycloak"}', set()),
        ('{"up": {"a": 1}}', set()),
        ('{"up": ["keycloak", 3, null, "", ["x"], "localai"]}', {"keycloak", "localai"}),
    ],
)
def test_what_is_not_a_list_of_names_is_ignored_in_the_remembered_services(
    tmp_path: Path, content: str, expected: set[str]
) -> None:
    path = store.state_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert store.load_state(str(tmp_path)).up == expected
