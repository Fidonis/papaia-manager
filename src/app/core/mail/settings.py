"""What the core stored for the mail relay, and the rules for changing it.

The core owns the configuration: `papaia-ctl setup --smtp ...` validates the values,
keeps them sticky, raises the password revision and edits `COMPOSE_PROFILES`
(`tools/lib/smtp.py` and `resolve.resolve_smtp`). The manager reads the result and
asks `setup` to change it (`app.core.ctl.run_smtp_setup`), so there is exactly one
writer.

The validators below mirror the core's so the page can answer a field at once and the
API can refuse a request before a job is queued. They are not trusted: `setup` checks
again, and where the two differ the core wins, which is why a refusal from `setup` is
shown to the operator in its own words. `tests/test_mail_contract.py` compares the
mirror with the core's own module (it needs `PAPAIA_SRC`, the core checkout).

The upstream password is never read here beyond "is there one": the file is mounted
into the relay and written by the core, and nothing in the manager has a reason to see
it. A stored password belongs to the host, port and login it was stored with and is
used for no others (`stored_secret_applies`), the same rule the stored keys of the
vector database connections follow.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.envfile import load_env_file
from app.core.inventory import profiles_in
from app.i18n import gettext as _

PROFILE = "smtp"
SERVICE = "smtp-relay"

# What every consumer of the relay is told: plain SMTP on this name and port, inside
# `papaia-net`. Fixed in the core because the realm block and the add-on contract name
# them.
RELAY_HOST = "smtp-relay"
RELAY_PORT = 587

SECURITY_MODES = ("starttls", "tls", "none")
DEFAULT_PORTS = {"starttls": 587, "tls": 465, "none": 25}

KEY_HOST = "SMTP_RELAY_UPSTREAM_HOST"
KEY_PORT = "SMTP_RELAY_UPSTREAM_PORT"
KEY_SECURITY = "SMTP_RELAY_UPSTREAM_SECURITY"
KEY_USER = "SMTP_RELAY_UPSTREAM_USER"
KEY_FROM = "SMTP_RELAY_FROM_ADDRESS"
KEY_FROM_NAME = "SMTP_RELAY_FROM_NAME"

DEFAULT_FROM_NAME = "papAIa"
MAX_PASSWORD_LENGTH = 512

_MODULE_DIR = Path("infra") / "smtp-relay"
_PASSWORD_FILE = "upstream-password"

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_ADDRESS_RE = re.compile(
    r"^[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+)$"
)
# The core writes the module env as KEY=value without quotes and Compose reads it back:
# these characters would change meaning on the way.
_ENV_UNSAFE = set("#$\"'\\`")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class MailError(ValueError):
    """A value the relay cannot take; the message says what to change."""


# ---------------------------------------------------------------------------
# Validation (mirror of tools/lib/smtp.py)
# ---------------------------------------------------------------------------


def validate_host(value: str) -> str:
    host = value.strip()
    if not host or not _HOST_RE.match(host):
        raise MailError(
            _(
                "'%(value)s' is not a valid mail server host name (letters, digits, dots and "
                "hyphens; no scheme, no port)."
            ) % {"value": value}
        )
    return host


def validate_port(value: str | int) -> int:
    try:
        port = int(str(value).strip())
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise MailError(_("'%(value)s' is not a valid port (1-65535).") % {"value": value})
    return port


def validate_security(value: str) -> str:
    mode = value.strip().lower()
    if mode not in SECURITY_MODES:
        raise MailError(
            _(
                "'%(value)s' is not a valid connection security (%(value2)s)."
            ) % {"value": value, "value2": ', '.join(SECURITY_MODES)}
        )
    return mode


def _env_safe(label: str, value: str) -> str:
    text = value.strip()
    if _CONTROL.search(text) or any(ch in _ENV_UNSAFE for ch in text):
        raise MailError(
            _(
                "%(label)s may not contain control characters or any of # $ \" ' \\ ` (they "
                "would change meaning in the .env file)."
            ) % {"label": label}
        )
    return text


def validate_user(value: str) -> str:
    user = _env_safe(_("The mail server login"), value)
    if len(user) > 256 or ":" in user:
        raise MailError(_("The mail server login may not contain ':' or exceed 256 characters."))
    return user


def validate_address(value: str) -> str:
    address = _env_safe(_("The sender address"), value)
    if not _ADDRESS_RE.match(address):
        raise MailError(_(
            "'%(value)s' is not a valid sender address (expected name@example.org)."
        ) % {"value": value})
    return address


def validate_display_name(value: str) -> str:
    name = _env_safe(_("The sender name"), value)
    if not name or len(name) > 80:
        raise MailError(_("The sender name must have 1-80 characters."))
    return name


def validate_password(value: str) -> str:
    """The upstream password as typed: any printable character, inner spaces included.

    Leading and trailing whitespace is refused because Postfix reads the credentials
    from a table whose values are stripped, so such a password would silently differ
    from the one the server expects."""
    if not value or len(value) > MAX_PASSWORD_LENGTH:
        raise MailError(_(
            "The mail server password must have 1-%(MAX_PASSWORD_LENGTH)s characters."
        ) % {"MAX_PASSWORD_LENGTH": MAX_PASSWORD_LENGTH})
    if any(ch in value for ch in "\r\n\x00"):
        raise MailError(_("The mail server password may not contain line breaks."))
    if value != value.strip():
        raise MailError(
            _(
                "The mail server password may not start or end with a space: the relay could not "
                "send it unchanged."
            )
        )
    return value


# ---------------------------------------------------------------------------
# What the core stored
# ---------------------------------------------------------------------------


def core_file(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "papaia" / "tools" / "lib" / "smtp.py"


def supported(workspace_dir: str) -> bool:
    """Whether the core ships the mail relay. Asked as the existence of its module in the
    workspace, like `host_health.is_supported` asks for `doctor`, not as a version."""
    return core_file(workspace_dir).is_file()


def module_env_path(config_dir: str) -> Path:
    return Path(config_dir) / _MODULE_DIR / ".env"


def password_path(config_dir: str) -> Path:
    return Path(config_dir) / _MODULE_DIR / _PASSWORD_FILE


def read_password(config_dir: str) -> str:
    """The stored upstream password, or "" when there is none. Only the connection test
    reads it, to try it against the server it was stored for (`stored_secret_applies`);
    it is not part of the settings and not returned by anything."""
    try:
        return password_path(config_dir).read_text(encoding="utf-8")
    except OSError:
        return ""


@dataclass(frozen=True)
class MailSettings:
    """The relay's configuration as setup stored it. Never carries the password."""

    profile_on: bool
    host: str = ""
    port: int | None = None
    security: str = "starttls"
    user: str = ""
    from_address: str = ""
    from_name: str = DEFAULT_FROM_NAME
    has_password: bool = False
    revision: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.host and self.from_address)

    @property
    def effective_port(self) -> int:
        return self.port if self.port is not None else DEFAULT_PORTS[self.security]


def load_mail_settings(config_dir: str) -> MailSettings:
    """Read the stored configuration. A missing file is a relay that was never set up."""
    root = load_env_file(Path(config_dir) / ".env")
    module = load_env_file(module_env_path(config_dir))
    raw_port = module.get(KEY_PORT, "").strip()
    security = module.get(KEY_SECURITY, "").strip().lower()
    try:
        has_password = password_path(config_dir).stat().st_size > 0
    except OSError:
        has_password = False
    return MailSettings(
        profile_on=PROFILE in profiles_in(root),
        host=module.get(KEY_HOST, "").strip(),
        port=int(raw_port) if raw_port.isdigit() else None,
        security=security if security in SECURITY_MODES else "starttls",
        user=module.get(KEY_USER, "").strip(),
        from_address=module.get(KEY_FROM, "").strip(),
        from_name=module.get(KEY_FROM_NAME, "").strip() or DEFAULT_FROM_NAME,
        has_password=has_password,
        revision=_revision(config_dir, profile_on=PROFILE in profiles_in(root)),
    )


def _revision(config_dir: str, *, profile_on: bool) -> str:
    """A fingerprint of what a form was built on, so a save from a stale tab is refused.

    The module env carries the password revision, so a changed password changes it too
    without the password file being read."""
    try:
        module_bytes = module_env_path(config_dir).read_bytes()
    except OSError:
        module_bytes = b""
    try:
        size = password_path(config_dir).stat().st_size
    except OSError:
        size = -1
    digest = hashlib.sha256()
    digest.update(module_bytes)
    digest.update(b"\0on" if profile_on else b"\0off")
    digest.update(str(size).encode())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# A request to change it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MailInput:
    """A validated request to switch the relay on or change it. `password` is None to keep
    the stored one and never appears in a repr."""

    host: str
    port: int
    security: str
    user: str
    from_address: str
    from_name: str
    password: str | None = field(default=None, repr=False)

    @property
    def login(self) -> bool:
        return bool(self.user)

    def password_action(self, current: MailSettings) -> str:
        """`set`, `kept`, `removed` or `none`, for the audit log: never the value."""
        if not self.user:
            return "removed" if current.has_password else "none"
        return "set" if self.password else "kept"


def validate_input(
    *,
    host: str,
    port: int | str | None,
    security: str,
    user: str | None,
    from_address: str,
    from_name: str | None,
    password: str | None,
    current: MailSettings,
) -> MailInput:
    """Check a request against the rules and against what is stored.

    Raises `MailError`. `port=None` takes the default of the chosen security. An empty
    `user` is a mail server without login. An empty or missing `password` keeps the
    stored one, but only for the host, port and login it was stored with."""
    mode = validate_security(security)
    clean_host = validate_host(host)
    clean_port = (
        DEFAULT_PORTS[mode] if port is None or str(port).strip() == "" else (validate_port(port))
    )
    clean_user = validate_user(user) if user and user.strip() else ""
    sender = validate_address(from_address)
    name = validate_display_name(
        from_name if from_name and from_name.strip() else DEFAULT_FROM_NAME
    )

    new_password = validate_password(password) if password else None
    if clean_user:
        if mode == "none":
            raise MailError(
                _(
                    "The login would be sent unencrypted: use starttls or tls, or clear the "
                    "login for a mail server that needs none."
                )
            )
        if new_password is None:
            if not current.has_password:
                raise MailError(_("A login needs its password: enter the password of the login."))
            if not stored_secret_applies(
                current, host=clean_host, port=clean_port, user=clean_user
            ):
                raise MailError(
                    _(
                        "The stored password belongs to another mail server or login: enter the "
                        "password again."
                    )
                )
    elif new_password is not None:
        raise MailError(_("A password needs a login: enter the login, or clear the password."))
    return MailInput(
        host=clean_host,
        port=clean_port,
        security=mode,
        user=clean_user,
        from_address=sender,
        from_name=name,
        password=new_password,
    )


def stored_secret_applies(current: MailSettings, *, host: str, port: int, user: str) -> bool:
    """Whether the stored password may be used for this host, port and login.

    Otherwise a request could send a password to a server of its choosing: the manager
    would be the one carrying it there."""
    return (
        current.has_password
        and bool(current.user)
        and current.user == user
        and current.host.lower() == host.lower()
        and current.effective_port == port
    )


def settings_json(current: MailSettings) -> dict[str, Any]:
    """The settings as the API and the page show them. No password, only whether there is one."""
    return {
        "enabled": current.profile_on,
        "configured": current.configured,
        "host": current.host,
        "port": current.effective_port,
        "security": current.security,
        "user": current.user,
        "login": bool(current.user),
        "has_password": current.has_password,
        "from_address": current.from_address,
        "from_name": current.from_name,
        "revision": current.revision,
    }
