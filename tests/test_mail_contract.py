"""The manager against the core's real code: what it asks of `setup` is what `setup` does.

The manager mirrors the core's rules for the mail relay and drives its `setup` command, so a drift
between the two is a form that accepts what `setup` refuses, or a `setup` run that changes more
than the mail settings. These tests load the core's own `lib.smtp`, and run its real `setup`
(through `app.core.ctl.run_smtp_setup`) in a throwaway copy of the checkout.

They need the core's checkout and are skipped without it. Point `PAPAIA_SRC` at a checkout of
`Fidonis/papaia` that has the mail relay (the `smtp` profile), for example
`C:\\Projects\\fidonis\\papaia`. Repeat them when either side's rules, option names or the
settings `setup` stores change.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SRC = os.environ.get("PAPAIA_SRC", "")

pytestmark = pytest.mark.skipif(
    not _SRC or not (Path(_SRC) / "tools" / "lib" / "smtp.py").is_file(),
    reason="PAPAIA_SRC does not point at a checkout of the core that has the mail relay",
)

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-mail-contract-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-mail-contract-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from app.core import ctl  # noqa: E402
from app.core.ctl import SmtpSetup, run_smtp_setup  # noqa: E402
from app.core.mail import service as mail_service  # noqa: E402
from app.core.mail import settings as mine  # noqa: E402
from app.core.mail.settings import MailError, load_mail_settings  # noqa: E402

_PASSWORD = "s3cret-pass-word-value"

_SMTP_FLAGS = SmtpSetup(
    enable=True,
    host="smtp.example.org",
    port=587,
    security="starttls",
    user="mailer",
    sender="noreply@example.org",
    sender_name="Our Mail",
    password=_PASSWORD,
)


@pytest.fixture(scope="module")
def core() -> Iterator[ModuleType]:
    """The core's own `lib.smtp`, imported off the checkout and forgotten afterwards."""
    tools = str(Path(_SRC) / "tools")
    sys.path.insert(0, tools)
    try:
        yield importlib.import_module("lib.smtp")
    finally:
        sys.path.remove(tools)
        for name in [n for n in sys.modules if n == "lib" or n.startswith("lib.")]:
            del sys.modules[name]


# -- the mirror ----------------------------------------------------------------------


def test_the_names_the_manager_reads_are_the_ones_the_core_writes(core: ModuleType) -> None:
    assert mine.PROFILE == core.PROFILE
    assert mine.SERVICE == core.SERVICE
    assert (mine.RELAY_HOST, mine.RELAY_PORT) == (core.RELAY_HOST, core.RELAY_PORT)
    assert mine.SECURITY_MODES == core.SECURITY_MODES
    assert mine.DEFAULT_PORTS == core.DEFAULT_PORTS
    assert mine.DEFAULT_FROM_NAME == core.DEFAULT_FROM_NAME
    assert mine.MAX_PASSWORD_LENGTH == core.MAX_PASSWORD_LENGTH
    for name in ("HOST", "PORT", "SECURITY", "USER", "FROM", "FROM_NAME"):
        assert getattr(mine, f"KEY_{name}") == getattr(core, f"KEY_{name}")
    assert mine.module_env_path("/c") == Path("/c") / core.MODULE_DIR / ".env"
    assert mine.password_path("/c") == Path("/c") / core.MODULE_DIR / core.PASSWORD_FILE


_CORPUS: dict[str, list[Any]] = {
    "host": [
        "smtp.example.org",
        " smtp.example.org ",
        "10.0.0.5",
        "a",
        "",
        "  ",
        "a b",
        "x/y",
        "smtp.example.org:587",
        "https://smtp.example.org",
        "-bad.example",
        "bad-.example",
        "a" * 253,
        "a" * 254,
        "under_score.example",
        "münchen.example",
    ],
    "port": ["587", 25, " 465 ", "65535", "0", "65536", "-1", "abc", "", "5.87", "٣"],
    "security": ["starttls", " STARTTLS ", "tls", "none", "ssl", "", "None"],
    "user": [
        "mailer",
        "user@example.org",
        "a:b",
        "with space",
        "dollar$",
        "hash#x",
        'quote"',
        "back`tick",
        "x" * 256,
        "x" * 257,
        "",
        "tab\there",
        "new\nline",
    ],
    "address": [
        "noreply@example.org",
        "a.b+c@mail.example.co.uk",
        "",
        "noreply",
        "noreply@localhost",
        "a b@example.org",
        "@example.org",
        "a@-example.org",
        "a@example..org",
        "x" * 65 + "@example.org",
        "$a@example.org",
    ],
    "display_name": ["Our Mail", " Our Mail ", "", "x" * 80, "x" * 81, "with # hash", "quote'x"],
    "password": [
        "plain",
        "with $ # \" ' \\ symbols",
        "inner space ok",
        "x" * 512,
        "x" * 513,
        "",
        " lead",
        "trail ",
        "line\nbreak",
        "cr\rbreak",
        "nul\x00",
        "tab\tinside",
    ],
}
_PAIRS = {
    "host": ("validate_host", "validate_host"),
    "port": ("validate_port", "validate_port"),
    "security": ("validate_security", "validate_security"),
    "user": ("validate_user", "validate_user"),
    "address": ("validate_address", "validate_address"),
    "display_name": ("validate_display_name", "validate_display_name"),
    "password": ("validate_password", "validate_password"),
}


def _outcome(function: Any, value: Any) -> tuple[str, Any]:
    try:
        return "ok", function(value)
    except (MailError, ValueError) as exc:
        return "refused", type(exc).__name__ and None


@pytest.mark.parametrize("field", sorted(_CORPUS))
def test_a_value_is_accepted_or_refused_exactly_as_the_core_does(
    core: ModuleType, field: str
) -> None:
    mine_name, core_name = _PAIRS[field]
    for value in _CORPUS[field]:
        ours = _outcome(getattr(mine, mine_name), value)
        theirs = _outcome(getattr(core, core_name), value)
        assert ours == theirs, f"{field}: {value!r}: manager {ours}, core {theirs}"


# -- the real `setup` ----------------------------------------------------------------


@pytest.fixture(scope="module")
def checkout_copy() -> Iterator[Path]:
    """A copy of the core's tools and compose files, so a `setup` run writes its `src/**/.env`
    into a directory of its own and never into the checkout under test."""
    root = Path(tempfile.mkdtemp(prefix="papaia-mail-contract-core-"))
    for name in ("tools", "src", "VERSION", "ADDON_API"):
        source = Path(_SRC) / name
        if source.is_dir():
            shutil.copytree(
                source,
                root / name,
                ignore=shutil.ignore_patterns(
                    ".venv",
                    "__pycache__",
                    ".pytest_cache",
                    ".ruff_cache",
                    "tests",
                    ".env",
                    "node_modules",
                ),
            )
        else:
            shutil.copy2(source, root / name)
    yield root
    shutil.rmtree(root, ignore_errors=True)


class _Deployment:
    """A throwaway deployment: a workspace holding a copy of the core, and a config directory."""

    def __init__(self, template: Path, base: Path) -> None:
        self.workspace = base / "workspace"
        self.repo = self.workspace / "papaia"
        shutil.copytree(template, self.repo)
        self.config = base / "config"

    def setup(self, *flags: str) -> None:
        import subprocess  # noqa: PLC0415

        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.repo / "tools")
        env["PAPAIA_CONFIG_DIR"] = str(self.config)
        env.pop("PAPAIA_SMTP_PASSWORD", None)
        done = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "lib.cli",
                "--repo-root",
                str(self.repo),
                "--config-dir",
                str(self.config),
                "setup",
                *flags,
            ],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(self.repo / "tools"),
            check=False,
        )
        assert done.returncode == 0, done.stderr[-800:]

    def snapshot(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for name, base in (("config", self.config), ("src", self.repo / "src")):
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    files[f"{name}:{path.relative_to(base).as_posix()}"] = path.read_bytes()
        return files

    def env(self, relative: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in (self.config / relative).read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        return values


@pytest.fixture
def deployment(checkout_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Deployment:
    # `run_smtp_setup` starts `python3`; this machine's interpreter is the one under test.
    original = asyncio.create_subprocess_exec

    async def spawn(*argv: str, **kwargs: Any) -> Any:
        first = sys.executable if argv and argv[0] == "python3" else argv[0]
        return await original(first, *argv[1:], **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    made = _Deployment(checkout_copy, tmp_path)
    made.setup("--app-host=https://papaia.test")
    return made


async def _apply(made: _Deployment, spec: SmtpSetup) -> tuple[int, str, str]:
    return await run_smtp_setup(
        spec, workspace_dir=str(made.workspace), config_dir=str(made.config), limit=120
    )


async def test_switching_the_relay_on_changes_the_mail_settings_and_what_follows_from_them(
    deployment: _Deployment,
) -> None:
    before = deployment.snapshot()
    before_root = deployment.env(".env")

    code, _, err = await _apply(deployment, _SMTP_FLAGS)
    assert code == 0, err

    after = deployment.snapshot()
    changed = {key for key in after if before.get(key) != after[key]}
    # Everything that moves is the mail relay's, or follows from it: the profile list in the
    # env files and in the manifest, the realm the profile adds a mail server to, and the
    # record of when setup last ran.
    assert changed == {
        "config:.env",
        "config:deployed.lock",
        "config:deployment.yaml",
        "config:infra/keycloak/realm-import/papaia-realm.json",
        "config:infra/smtp-relay/.env",
        "config:infra/smtp-relay/upstream-password",
        "src:.env",
        "src:infra/smtp-relay/.env",
    }
    after_root = deployment.env(".env")
    moved = {
        k for k in set(before_root) | set(after_root) if before_root.get(k) != after_root.get(k)
    }
    assert moved == {"COMPOSE_PROFILES"}
    assert "smtp" in after_root["COMPOSE_PROFILES"].split(",")
    assert set(after_root["COMPOSE_PROFILES"].split(",")) - {"smtp"} == set(
        before_root["COMPOSE_PROFILES"].split(",")
    )
    assert "smtp" in (deployment.config / "deployment.yaml").read_text(encoding="utf-8")


async def test_a_named_environment_and_its_addresses_survive_a_run_that_only_names_the_relay(
    checkout_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = asyncio.create_subprocess_exec

    async def spawn(*argv: str, **kwargs: Any) -> Any:
        first = sys.executable if argv and argv[0] == "python3" else argv[0]
        return await original(first, *argv[1:], **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    made = _Deployment(checkout_copy, tmp_path)
    made.setup("--env=staging", "--host-ip=10.1.2.3", "--app-host=https://staging.test")
    before = made.env(".env")
    assert before["COMPOSE_PROJECT_NAME"] == "papaia-staging"

    code, _, err = await _apply(made, _SMTP_FLAGS)
    assert code == 0, err
    after = made.env(".env")
    assert {k for k in set(before) | set(after) if before.get(k) != after.get(k)} == {
        "COMPOSE_PROFILES"
    }


async def test_values_an_operator_edited_by_hand_survive_the_run(deployment: _Deployment) -> None:
    # A port moved off its default, a variable of an add-on and one of the operator's own: the
    # run names the relay's options only, and nothing else in the files may move.
    root = deployment.config / ".env"
    text = root.read_text(encoding="utf-8")
    assert "KEYCLOAK_EXT_PORT=" in text
    lines = [
        "KEYCLOAK_EXT_PORT=18999" if line.startswith("KEYCLOAK_EXT_PORT=") else line
        for line in text.splitlines()
    ]
    root.write_text("\n".join([*lines, "MY_OWN_SETTING=kept", ""]), encoding="utf-8")
    before = deployment.env(".env")

    code, _, err = await _apply(deployment, _SMTP_FLAGS)

    assert code == 0, err
    after = deployment.env(".env")
    assert after["KEYCLOAK_EXT_PORT"] == "18999"
    assert after["MY_OWN_SETTING"] == "kept"
    assert {k for k in set(before) | set(after) if before.get(k) != after.get(k)} == {
        "COMPOSE_PROFILES"
    }


async def test_what_setup_stored_is_what_the_manager_reads(deployment: _Deployment) -> None:
    await _apply(deployment, _SMTP_FLAGS)
    stored = load_mail_settings(str(deployment.config))
    assert stored.profile_on and stored.configured and stored.has_password
    assert (stored.host, stored.port, stored.security, stored.user) == (
        "smtp.example.org",
        587,
        "starttls",
        "mailer",
    )
    assert (stored.from_address, stored.from_name) == ("noreply@example.org", "Our Mail")
    password_file = deployment.config / "infra" / "smtp-relay" / "upstream-password"
    assert password_file.read_text(encoding="utf-8") == _PASSWORD
    if os.name == "posix":
        assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    # The password is in that file and nowhere else setup wrote.
    for relative, content in deployment.snapshot().items():
        if not relative.endswith("upstream-password"):
            assert _PASSWORD.encode() not in content, relative


async def test_the_same_values_again_change_nothing_but_the_time_setup_ran(
    deployment: _Deployment,
) -> None:
    await _apply(deployment, _SMTP_FLAGS)
    first = deployment.snapshot()
    kept = SmtpSetup(**{**_SMTP_FLAGS.__dict__, "password": None})
    code, _, err = await _apply(deployment, kept)
    assert code == 0, err
    second = deployment.snapshot()
    # The record of when setup ran has a resolution of a second: it moves, unless both runs
    # fell into the same one.
    assert {key for key in second if first.get(key) != second[key]} <= {"config:deployed.lock"}
    assert load_mail_settings(str(deployment.config)).has_password


async def test_a_new_password_raises_the_revision_so_the_relay_is_recreated(
    deployment: _Deployment,
) -> None:
    await _apply(deployment, _SMTP_FLAGS)
    module = "infra/smtp-relay/.env"
    first = deployment.env(module)["SMTP_RELAY_PASSWORD_REV"]
    await _apply(deployment, SmtpSetup(**{**_SMTP_FLAGS.__dict__, "password": "another-one"}))
    assert int(deployment.env(module)["SMTP_RELAY_PASSWORD_REV"]) == int(first) + 1


async def test_switching_off_removes_the_profile_and_keeps_the_settings_and_the_password(
    deployment: _Deployment,
) -> None:
    await _apply(deployment, _SMTP_FLAGS)
    code, _, err = await _apply(deployment, SmtpSetup(enable=False))
    assert code == 0, err
    stored = load_mail_settings(str(deployment.config))
    assert not stored.profile_on
    assert stored.configured and stored.has_password and stored.host == "smtp.example.org"


async def test_a_login_that_is_dropped_takes_the_stored_password_with_it(
    deployment: _Deployment,
) -> None:
    await _apply(deployment, _SMTP_FLAGS)
    spec = SmtpSetup(**{**_SMTP_FLAGS.__dict__, "user": None, "no_auth": True, "password": None})
    code, _, err = await _apply(deployment, spec)
    assert code == 0, err
    stored = load_mail_settings(str(deployment.config))
    assert stored.user == "" and not stored.has_password


async def test_a_value_the_core_refuses_is_a_sentence_and_writes_nothing(
    deployment: _Deployment,
) -> None:
    before = deployment.snapshot()
    spec = SmtpSetup(**{**_SMTP_FLAGS.__dict__, "host": "bad host"})
    code, out, err = await _apply(deployment, spec)
    assert code == 3
    sentence = mail_service.refusal(code, out, err)
    assert "bad host" in sentence and "not a valid mail server host name" in sentence
    assert deployment.snapshot() == before


def test_the_flags_the_manager_passes_are_options_the_core_knows(
    core: ModuleType, checkout_copy: Path
) -> None:
    text = (checkout_copy / "tools" / "lib" / "cli.py").read_text(encoding="utf-8")
    for flag in ctl.smtp_setup_flags(_SMTP_FLAGS):
        option = flag.split("=", 1)[0]
        assert f'"{option}"' in text, option
    assert '"--smtp-no-auth"' in text
    assert ctl.SMTP_PASSWORD_ENV == "PAPAIA_SMTP_PASSWORD"
    assert "PAPAIA_SMTP_PASSWORD" in text
