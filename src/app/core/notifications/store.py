"""`manager/notifications.yaml` (what an administrator configured) and
`manager/notifications-state.json` (what the manager remembers so a restart repeats nothing).

The two are separate on purpose. The YAML is edited from the page, may be edited by hand and is
checked against a revision, so a stale tab cannot overwrite a newer change. The state is written
by the background task alone: a pass must be able to record that it sent something without
making every open page stale.

The YAML holds webhook URLs, which authorise whoever posts to them: it is written with mode
0600 and never returned by anything in full. The configuration directory is backed up as a
whole, so the URLs travel with every backup like the stack's other secrets.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.core.notifications.model import (
    Event,
    EventSettings,
    Kind,
    NotificationsConfig,
    NotificationsError,
    Recipient,
    validate_events,
    validate_name,
    validate_target,
)
from app.core.vectordb.catalog_io import revision as _revision
from app.i18n import gettext as _

logger = logging.getLogger(__name__)

CONFIG_NAME = "notifications.yaml"
STATE_NAME = "notifications-state.json"

_HEADER = (
    "# Written by the papAIa manager (Mail > Notifications).\n"
    "# Webhook URLs authorise whoever posts to them: keep this file private (mode 0600).\n"
)

# One lock for the read-modify-write of each file. A single process writes them (one worker),
# the same assumption the audit log makes.
_CONFIG_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()


class StaleRevisionError(Exception):
    """The file changed since the page that asked for this change was loaded."""


class ConfigUnreadableError(Exception):
    """The file is there but cannot be used. A write would replace what an administrator
    wrote by hand, so there is none until it is repaired."""


def config_path(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / CONFIG_NAME


def state_path(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / STATE_NAME


@dataclass(frozen=True)
class ConfigLoad:
    """The configuration, its revision, and why it is empty if the file is unusable."""

    config: NotificationsConfig
    revision: str
    error: str | None = None


def load_config(config_dir: str) -> ConfigLoad:
    """Read the configuration. Never raises: a missing file is an empty configuration, a damaged
    one is an empty configuration with `error` set (and is shown, not silently replaced)."""
    path = config_path(config_dir)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return ConfigLoad(NotificationsConfig(), "")
    except OSError as exc:
        logger.warning("%s cannot be read: %s", path, exc)
        return ConfigLoad(
            NotificationsConfig(), "", _("%(name)s cannot be read.") % {"name": CONFIG_NAME}
        )
    revision = _revision(raw_bytes)
    try:
        raw = yaml.safe_load(raw_bytes)
        config = NotificationsConfig.model_validate(raw if raw is not None else {})
    except (yaml.YAMLError, ValidationError) as exc:
        # The log may name the problem; the page gets a sentence that repeats no value.
        logger.warning("%s is not usable: %s", path, type(exc).__name__)
        return ConfigLoad(
            NotificationsConfig(),
            revision,
            _("%(name)s is not valid and is ignored.") % {"name": CONFIG_NAME},
        )
    return ConfigLoad(config, revision)


def _write_private(path: Path, data: bytes) -> None:
    """Replace `path` with `data`, readable by the owner only from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    # O_BINARY: on Windows a descriptor is otherwise opened in text mode.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _dump(config: NotificationsConfig) -> bytes:
    body = yaml.safe_dump(
        config.model_dump(mode="json"),
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    return (_HEADER + body).encode("utf-8")


def mutate(
    config_dir: str, revision: str, change: Callable[[NotificationsConfig], None]
) -> ConfigLoad:
    """Apply `change` to the configuration as it is now, if it is still the one `revision`
    names, and store the result. Raises `StaleRevisionError`, `ConfigUnreadableError` or
    `NotificationsError` (the change, or the result of it, is refused); nothing is written
    then."""
    with _CONFIG_LOCK:
        current = load_config(config_dir)
        if current.error:
            raise ConfigUnreadableError(current.error)
        if current.revision != revision:
            raise StaleRevisionError
        draft = current.config.model_copy(deep=True)
        change(draft)
        # The change may have broken a rule that only the whole configuration knows (two
        # names alike): validate the result the way a file would be.
        try:
            checked = NotificationsConfig.model_validate(draft.model_dump())
        except ValidationError as exc:
            problems = [
                str(err.get("ctx", {}).get("error", "")).strip() or str(err["msg"])
                for err in exc.errors()
            ]
            first = problems[0] if problems else _("The change is not valid.")
            raise NotificationsError(first) from exc
        data = _dump(checked)
        _write_private(config_path(config_dir), data)
        return ConfigLoad(checked, _revision(data))


def add_recipient(
    config_dir: str,
    revision: str,
    *,
    name: str,
    kind: Kind,
    target: str,
    enabled: bool,
    events: list[Event],
) -> tuple[ConfigLoad, Recipient]:
    created: list[Recipient] = []

    def change(config: NotificationsConfig) -> None:
        recipient = Recipient(
            id=uuid.uuid4().hex[:8],
            name=validate_name(name),
            kind=kind,
            target=validate_target(kind, target),
            enabled=enabled,
            events=validate_events(events),
        )
        config.recipients.append(recipient)
        created.append(recipient)

    result = mutate(config_dir, revision, change)
    return result, created[0]


def update_recipient(
    config_dir: str,
    revision: str,
    recipient_id: str,
    *,
    name: str | None = None,
    target: str | None = None,
    enabled: bool | None = None,
    events: list[Event] | None = None,
) -> tuple[ConfigLoad, Recipient]:
    """Change what is given. A `target` left out keeps the stored one (a webhook URL is
    write-only, so a form cannot send it back)."""
    updated: list[Recipient] = []

    def change(config: NotificationsConfig) -> None:
        recipient = config.find(recipient_id)
        if recipient is None:
            raise LookupError(recipient_id)
        if name is not None:
            recipient.name = validate_name(name)
        if target:
            recipient.target = validate_target(recipient.kind, target)
        if enabled is not None:
            recipient.enabled = enabled
        if events is not None:
            recipient.events = validate_events(events)
        updated.append(recipient)

    result = mutate(config_dir, revision, change)
    return result, updated[0]


def delete_recipient(config_dir: str, revision: str, recipient_id: str) -> tuple[ConfigLoad, str]:
    """Remove a recipient. Its subscriptions are part of it, so nothing is left behind."""
    removed: list[str] = []

    def change(config: NotificationsConfig) -> None:
        recipient = config.find(recipient_id)
        if recipient is None:
            raise LookupError(recipient_id)
        config.recipients.remove(recipient)
        removed.append(recipient.name)

    result = mutate(config_dir, revision, change)
    forget_recipient(config_dir, recipient_id)
    return result, removed[0]


def save_settings(config_dir: str, revision: str, settings: EventSettings) -> ConfigLoad:
    def change(config: NotificationsConfig) -> None:
        config.settings = settings

    return mutate(config_dir, revision, change)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class Seen:
    """What the manager remembers about one condition (`<event>|<key>`)."""

    active: bool = False
    marker: str = ""
    last_sent: str = ""


@dataclass
class TestResult:
    at: str
    ok: bool
    reason: str = ""


@dataclass
class NotifyState:
    seen: dict[str, Seen] = field(default_factory=dict)
    tests: dict[str, TestResult] = field(default_factory=dict)
    # The services a pass saw running: what makes one that is gone (stopped with "Clean up",
    # removed) an outage and not a module that was never started. See `detect.service_findings`.
    up: set[str] = field(default_factory=set)


def _state_from(raw: Any) -> NotifyState:
    state = NotifyState()
    if not isinstance(raw, dict):
        return state
    up = raw.get("up")
    if isinstance(up, list):
        state.up = {entry for entry in up if isinstance(entry, str) and entry}
    for key, entry in (raw.get("seen") or {}).items():
        if isinstance(entry, dict):
            state.seen[str(key)] = Seen(
                active=bool(entry.get("active", False)),
                marker=str(entry.get("marker", "")),
                last_sent=str(entry.get("last_sent", "")),
            )
    for key, entry in (raw.get("tests") or {}).items():
        if isinstance(entry, dict) and entry.get("at"):
            state.tests[str(key)] = TestResult(
                at=str(entry["at"]),
                ok=bool(entry.get("ok", False)),
                reason=str(entry.get("reason", "")),
            )
    return state


def load_state(config_dir: str) -> NotifyState:
    """Read the state. Never raises: what cannot be read is forgotten, which at worst repeats
    a notification once."""
    try:
        return _state_from(json.loads(state_path(config_dir).read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NotifyState()
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning("%s cannot be read: %s", STATE_NAME, type(exc).__name__)
        return NotifyState()


def update_state[T](config_dir: str, change: Callable[[NotifyState], T]) -> T:
    """Run `change` on the state and store it, under the lock. Returns what `change` returns."""
    with _STATE_LOCK:
        state = load_state(config_dir)
        result = change(state)
        document = {
            "version": 1,
            "seen": {k: asdict(v) for k, v in state.seen.items()},
            "tests": {k: asdict(v) for k, v in state.tests.items()},
            "up": sorted(state.up),
        }
        try:
            _write_private(state_path(config_dir), json.dumps(document, indent=1).encode("utf-8"))
        except OSError as exc:
            # A state that cannot be stored costs a repeat after the next restart, not the pass.
            logger.warning("%s cannot be written: %s", STATE_NAME, type(exc).__name__)
        return result


def forget_recipient(config_dir: str, recipient_id: str) -> None:
    """Drop what is remembered about a recipient that no longer exists."""

    def change(state: NotifyState) -> None:
        state.tests.pop(recipient_id, None)

    if recipient_id in load_state(config_dir).tests:
        update_state(config_dir, change)
