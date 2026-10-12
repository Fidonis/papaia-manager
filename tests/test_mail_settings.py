"""The mail relay's stored settings and the rules for changing them.

The rules mirror the core's (`tools/lib/smtp.py`); `test_mail_contract.py` compares the two when
a core checkout is at hand. What is pinned here is what the manager promises on its own: that a
missing file is a relay that was never set up, that the password is never read into the
settings, and that a stored password goes only where it was stored.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.mail.settings import (
    DEFAULT_PORTS,
    MailError,
    MailSettings,
    load_mail_settings,
    module_env_path,
    password_path,
    read_password,
    settings_json,
    stored_secret_applies,
    supported,
    validate_address,
    validate_display_name,
    validate_host,
    validate_input,
    validate_password,
    validate_port,
    validate_security,
    validate_user,
)


def _write(config_dir: Path, root: str = "", module: str = "", password: str | None = None) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text(root, encoding="utf-8")
    module_dir = config_dir / "infra" / "smtp-relay"
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / ".env").write_text(module, encoding="utf-8")
    if password is not None:
        (module_dir / "upstream-password").write_text(password, encoding="utf-8")


_MODULE = (
    "SMTP_RELAY_UPSTREAM_HOST=smtp.example.org\n"
    "SMTP_RELAY_UPSTREAM_PORT=587\n"
    "SMTP_RELAY_UPSTREAM_SECURITY=starttls\n"
    "SMTP_RELAY_UPSTREAM_USER=mailer\n"
    "SMTP_RELAY_FROM_ADDRESS=noreply@example.org\n"
    "SMTP_RELAY_FROM_NAME=Our Mail\n"
    "SMTP_RELAY_PASSWORD_REV=3\n"
)


# -- what the core stored ------------------------------------------------------------


def test_a_relay_that_was_never_set_up_reads_as_off_and_empty(tmp_path: Path) -> None:
    settings = load_mail_settings(str(tmp_path / "missing"))
    assert not settings.profile_on
    assert not settings.configured
    assert (settings.host, settings.user, settings.from_address) == ("", "", "")
    assert settings.from_name == "papAIa"
    assert settings.security == "starttls"
    assert settings.effective_port == 587
    assert not settings.has_password


def test_the_stored_values_are_read_from_the_profile_list_and_the_module_env(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "COMPOSE_PROFILES=nginx,smtp,rag\n", _MODULE, password="pw")
    settings = load_mail_settings(str(tmp_path))
    assert settings.profile_on
    assert settings.configured
    assert (settings.host, settings.port, settings.security) == (
        "smtp.example.org",
        587,
        "starttls",
    )
    assert (settings.user, settings.from_address, settings.from_name) == (
        "mailer",
        "noreply@example.org",
        "Our Mail",
    )
    assert settings.has_password


def test_the_profile_is_matched_by_name_not_by_substring(tmp_path: Path) -> None:
    _write(tmp_path, "COMPOSE_PROFILES=smtp-other,nginx\n", _MODULE)
    assert not load_mail_settings(str(tmp_path)).profile_on


def test_a_nonsense_security_or_port_falls_back_instead_of_failing(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "COMPOSE_PROFILES=smtp\n",
        "SMTP_RELAY_UPSTREAM_SECURITY=ssl\nSMTP_RELAY_UPSTREAM_PORT=abc\n",
    )
    settings = load_mail_settings(str(tmp_path))
    assert settings.security == "starttls"
    assert settings.port is None
    assert settings.effective_port == 587


def test_the_default_port_follows_the_security_when_none_is_stored() -> None:
    assert MailSettings(profile_on=True, security="tls").effective_port == DEFAULT_PORTS["tls"]
    assert MailSettings(profile_on=True, security="none").effective_port == 25


def test_the_password_is_only_ever_a_yes_or_no(tmp_path: Path) -> None:
    _write(tmp_path, "COMPOSE_PROFILES=smtp\n", _MODULE, password="hunter2-very-secret")
    settings = load_mail_settings(str(tmp_path))
    assert settings.has_password
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings_json(settings))
    assert "password" not in {key for key in settings_json(settings) if key != "has_password"}


def test_an_empty_password_file_is_no_password(tmp_path: Path) -> None:
    # The core writes the file empty for a server without a login.
    _write(tmp_path, "COMPOSE_PROFILES=smtp\n", _MODULE, password="")
    assert not load_mail_settings(str(tmp_path)).has_password


def test_only_the_connection_test_reads_the_password_itself(tmp_path: Path) -> None:
    _write(tmp_path, "", _MODULE, password="pw with spaces")
    assert read_password(str(tmp_path)) == "pw with spaces"
    assert read_password(str(tmp_path / "nowhere")) == ""
    assert password_path(str(tmp_path)).name == "upstream-password"
    assert module_env_path(str(tmp_path)).parent == password_path(str(tmp_path)).parent


def test_support_is_the_presence_of_the_cores_module(tmp_path: Path) -> None:
    assert not supported(str(tmp_path))
    lib = tmp_path / "papaia" / "tools" / "lib"
    lib.mkdir(parents=True)
    (lib / "smtp.py").write_text("", encoding="utf-8")
    assert supported(str(tmp_path))


# -- the revision --------------------------------------------------------------------


def test_the_revision_changes_with_the_module_env_the_profile_and_the_password_file(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "COMPOSE_PROFILES=nginx\n", _MODULE, password="a")
    base = load_mail_settings(str(tmp_path)).revision
    assert base == load_mail_settings(str(tmp_path)).revision

    _write(tmp_path, "COMPOSE_PROFILES=nginx,smtp\n", _MODULE, password="a")
    switched_on = load_mail_settings(str(tmp_path)).revision
    assert switched_on != base

    _write(
        tmp_path, "COMPOSE_PROFILES=nginx,smtp\n", _MODULE.replace("REV=3", "REV=4"), password="a"
    )
    assert load_mail_settings(str(tmp_path)).revision != switched_on

    _write(tmp_path, "COMPOSE_PROFILES=nginx,smtp\n", _MODULE, password="")
    assert load_mail_settings(str(tmp_path)).revision != switched_on


# -- the field rules -----------------------------------------------------------------


@pytest.mark.parametrize("host", ["smtp.example.org", "10.0.0.5", "mail-1.lan", "a"])
def test_a_host_name_or_address_is_accepted(host: str) -> None:
    assert validate_host(f"  {host} ") == host


@pytest.mark.parametrize(
    "host",
    ["", "  ", "smtp.example.org:587", "https://smtp.example.org", "-bad.example", "a b", "x/y"],
)
def test_a_host_with_a_scheme_a_port_or_junk_is_refused(host: str) -> None:
    with pytest.raises(MailError):
        validate_host(host)


@pytest.mark.parametrize(
    ("value", "port"), [("587", 587), (25, 25), (" 465 ", 465), ("65535", 65535)]
)
def test_a_port_is_a_number_between_1_and_65535(value: str | int, port: int) -> None:
    assert validate_port(value) == port


@pytest.mark.parametrize("value", ["0", "65536", "-1", "abc", "", "5.87"])
def test_anything_else_is_not_a_port(value: str) -> None:
    with pytest.raises(MailError):
        validate_port(value)


def test_the_security_is_one_of_three_and_case_insensitive() -> None:
    assert validate_security(" STARTTLS ") == "starttls"
    assert validate_security("tls") == "tls"
    assert validate_security("none") == "none"
    with pytest.raises(MailError):
        validate_security("ssl")


@pytest.mark.parametrize("user", ["mailer", "user@example.org", "a.b-c_d"])
def test_a_login_is_accepted(user: str) -> None:
    assert validate_user(user) == user


@pytest.mark.parametrize(
    "user", ["a:b", "with space#hash", "dollar$", 'quote"', "back`tick", "x" * 257]
)
def test_a_login_that_would_change_meaning_in_the_env_file_is_refused(user: str) -> None:
    with pytest.raises(MailError):
        validate_user(user)


@pytest.mark.parametrize("address", ["noreply@example.org", "a.b+c@mail.example.co.uk"])
def test_a_sender_address_is_accepted(address: str) -> None:
    assert validate_address(address) == address


@pytest.mark.parametrize(
    "address", ["", "noreply", "noreply@localhost", "a b@example.org", "@example.org"]
)
def test_a_sender_address_needs_a_name_and_a_dotted_domain(address: str) -> None:
    with pytest.raises(MailError):
        validate_address(address)


def test_a_display_name_has_one_to_eighty_characters() -> None:
    assert validate_display_name(" Our Mail ") == "Our Mail"
    with pytest.raises(MailError):
        validate_display_name("x" * 81)


@pytest.mark.parametrize(
    "password", ["plain", "with $ # \" ' \\ symbols", "inner space ok", "x" * 512]
)
def test_a_password_may_hold_any_printable_character(password: str) -> None:
    assert validate_password(password) == password


@pytest.mark.parametrize("password", ["", " lead", "trail ", "line\nbreak", "nul\x00", "x" * 513])
def test_a_password_the_relay_could_not_send_unchanged_is_refused(password: str) -> None:
    with pytest.raises(MailError):
        validate_password(password)


# -- a whole request -----------------------------------------------------------------


def _stored(**changes: object) -> MailSettings:
    base: dict[str, object] = {
        "profile_on": True,
        "host": "smtp.example.org",
        "port": 587,
        "security": "starttls",
        "user": "mailer",
        "from_address": "noreply@example.org",
        "from_name": "Our Mail",
        "has_password": True,
        "revision": "r",
    }
    base.update(changes)
    return MailSettings(**base)  # type: ignore[arg-type]


def _request(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "host": "smtp.example.org",
        "port": 587,
        "security": "starttls",
        "user": "mailer",
        "from_address": "noreply@example.org",
        "from_name": "Our Mail",
        "password": None,
        "current": _stored(),
    }
    base.update(changes)
    return base


def test_a_request_that_keeps_the_stored_password_is_valid_for_the_same_server() -> None:
    data = validate_input(**_request())  # type: ignore[arg-type]
    assert data.password is None
    assert data.password_action(_stored()) == "kept"


def test_a_typed_password_is_carried_and_never_shown_by_the_repr() -> None:
    data = validate_input(**_request(password="a-new-secret"))  # type: ignore[arg-type]
    assert data.password == "a-new-secret"
    assert data.password_action(_stored()) == "set"
    assert "a-new-secret" not in repr(data)


@pytest.mark.parametrize(
    "change",
    [{"host": "other.example.org"}, {"port": 465}, {"user": "someone-else"}],
)
def test_a_stored_password_is_not_used_for_another_server_port_or_login(
    change: dict[str, object],
) -> None:
    with pytest.raises(MailError, match="enter the password again"):
        validate_input(**_request(**change))  # type: ignore[arg-type]


def test_the_host_comparison_ignores_case() -> None:
    data = validate_input(**_request(host="SMTP.Example.ORG"))  # type: ignore[arg-type]
    assert data.host == "SMTP.Example.ORG"


def test_a_login_without_any_password_is_refused() -> None:
    with pytest.raises(MailError, match="needs its password"):
        validate_input(**_request(current=_stored(has_password=False)))  # type: ignore[arg-type]


def test_no_login_means_no_user_and_the_stored_password_goes() -> None:
    data = validate_input(**_request(user="", security="none", port=None))  # type: ignore[arg-type]
    assert (data.user, data.port, data.login) == ("", 25, False)
    assert data.password_action(_stored()) == "removed"


def test_a_password_without_a_login_is_refused() -> None:
    with pytest.raises(MailError, match="needs a login"):
        validate_input(**_request(user="", password="x"))  # type: ignore[arg-type]


def test_a_login_over_no_encryption_is_refused() -> None:
    with pytest.raises(MailError, match="unencrypted"):
        validate_input(**_request(security="none", port=25, password="x"))  # type: ignore[arg-type]


def test_an_omitted_port_takes_the_default_of_the_chosen_security() -> None:
    data = validate_input(**_request(security="tls", port=None, password="x"))  # type: ignore[arg-type]
    assert data.port == 465


def test_an_empty_sender_name_becomes_the_default() -> None:
    data = validate_input(**_request(from_name="  "))  # type: ignore[arg-type]
    assert data.from_name == "papAIa"


def test_the_stored_password_rule_needs_a_stored_login_and_password() -> None:
    assert stored_secret_applies(_stored(), host="smtp.example.org", port=587, user="mailer")
    assert not stored_secret_applies(
        _stored(has_password=False), host="smtp.example.org", port=587, user="mailer"
    )
    assert not stored_secret_applies(_stored(user=""), host="smtp.example.org", port=587, user="")


def test_the_json_of_the_settings_has_no_password_and_a_port_to_show() -> None:
    document = settings_json(_stored(port=None))
    assert document["port"] == 587
    assert document["has_password"] is True
    assert document["login"] is True
    assert document["revision"] == "r"
