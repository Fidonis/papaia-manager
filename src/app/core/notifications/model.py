"""The events, the recipients and the rules a recipient is checked against.

Every refusal is a `NotificationsError` with a sentence an administrator can act on, and
none of them repeats the value it refused: a webhook URL is a secret, and an address is
somebody's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.mail.probe import validate_recipient
from app.i18n import N_
from app.i18n import gettext as _

MAX_RECIPIENTS = 50
MAX_NAME_LENGTH = 60
MAX_URL_LENGTH = 2048

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class NotificationsError(ValueError):
    """A value is refused. The message is for the person who typed it."""


class Event(StrEnum):
    JOB_FAILED = "job_failed"
    BACKUP_OVERDUE = "backup_overdue"
    SERVICE_UNHEALTHY = "service_unhealthy"
    UPDATE_AVAILABLE = "update_available"
    CERT_EXPIRING = "cert_expiring"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Kind(StrEnum):
    EMAIL = "email"
    WEBHOOK = "webhook"


@dataclass(frozen=True)
class EventInfo:
    event: Event
    label: str
    # The column header of the matrix, broken where the label would otherwise be wide.
    short: str
    description: str
    severity: Severity


# Order is the order of the columns.
CATALOGUE: tuple[EventInfo, ...] = (
    EventInfo(
        Event.JOB_FAILED,
        N_("Job failed"),
        N_("Job\nfailed"),
        N_("a job ended with an error"),
        Severity.ERROR,
    ),
    EventInfo(
        Event.BACKUP_OVERDUE,
        N_("Backup overdue"),
        N_("Backup\noverdue"),
        N_("no good backup within the schedule"),
        Severity.ERROR,
    ),
    EventInfo(
        Event.SERVICE_UNHEALTHY,
        N_("Service unhealthy"),
        N_("Service\nunhealthy"),
        N_("a container stopped or reports unhealthy"),
        Severity.ERROR,
    ),
    EventInfo(
        Event.UPDATE_AVAILABLE,
        N_("Update available"),
        N_("Update\navailable"),
        N_("a newer papAIa or add-on version exists"),
        Severity.INFO,
    ),
    EventInfo(
        Event.CERT_EXPIRING,
        N_("Certificate expiring"),
        N_("Certificate\nexpiring"),
        N_("a TLS certificate runs out soon"),
        Severity.WARNING,
    ),
)

INFO_BY_EVENT: dict[Event, EventInfo] = {info.event: info for info in CATALOGUE}


def validate_name(value: str) -> str:
    name = " ".join(value.split())
    if not name:
        raise NotificationsError(_("Enter a name."))
    if len(name) > MAX_NAME_LENGTH:
        raise NotificationsError(_(
            "The name is longer than %(MAX_NAME_LENGTH)s characters."
        ) % {"MAX_NAME_LENGTH": MAX_NAME_LENGTH})
    return name


def validate_webhook_url(value: str) -> str:
    """An http(s) URL with a host and no login. Redirects are not followed at delivery, so a
    URL that only redirects is a refusal there, not a surprise."""
    url = value.strip()
    if not url:
        raise NotificationsError(_("Enter the webhook URL."))
    if len(url) > MAX_URL_LENGTH or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise NotificationsError(_("The webhook URL is too long or contains spaces."))
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 -- raises ValueError for a port that is not a number
    except ValueError as exc:
        raise NotificationsError(_("The webhook URL is not valid.")) from exc
    if parts.scheme not in ("http", "https"):
        raise NotificationsError(_("The webhook URL must start with http:// or https://."))
    if not parts.hostname:
        raise NotificationsError(_("The webhook URL has no host."))
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise NotificationsError(
            _("The webhook URL must not contain a login. Put a secret in the path or the query.")
        )
    return url


def validate_target(kind: Kind, value: str) -> str:
    if kind is Kind.EMAIL:
        try:
            return validate_recipient(value)
        except ValueError as exc:
            raise NotificationsError(_("Enter an email address like name@example.org.")) from exc
    return validate_webhook_url(value)


def validate_events(values: list[Event]) -> list[Event]:
    """Each event once, in the order of the catalogue."""
    chosen = set(values)
    return [info.event for info in CATALOGUE if info.event in chosen]


def mask_url(url: str) -> str:
    """Where a webhook goes, without the part that authorises it: `https://host/…`."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    shown = f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
    return shown + "/…" if parts.path.strip("/") or parts.query else shown


class Recipient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    kind: Kind
    # The address of an email recipient, the URL of a webhook.
    target: str
    enabled: bool = True
    events: list[Event] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Recipient:
        if not _ID_RE.match(self.id):
            raise NotificationsError(_("A recipient id is lowercase letters, digits and dashes."))
        self.name = validate_name(self.name)
        self.target = validate_target(self.kind, self.target)
        self.events = validate_events(self.events)
        return self

    @property
    def display_target(self) -> str:
        return mask_url(self.target) if self.kind is Kind.WEBHOOK else self.target


class EventSettings(BaseModel):
    """What applies to every recipient: how often the conditions are looked at, and the two
    numbers an event needs. A failed job is reported at once and has none."""

    model_config = ConfigDict(extra="forbid")

    check_interval_minutes: int = Field(default=5, ge=1, le=60)
    debounce_minutes: int = Field(default=30, ge=1, le=1440)
    cert_lead_days: int = Field(default=14, ge=1, le=90)


class NotificationsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    settings: EventSettings = Field(default_factory=EventSettings)
    recipients: list[Recipient] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> NotificationsConfig:
        if len(self.recipients) > MAX_RECIPIENTS:
            raise NotificationsError(_(
                "There can be at most %(MAX_RECIPIENTS)s recipients."
            ) % {"MAX_RECIPIENTS": MAX_RECIPIENTS})
        if len({r.id for r in self.recipients}) != len(self.recipients):
            raise NotificationsError(_("Two recipients have the same id."))
        if len({r.name.casefold() for r in self.recipients}) != len(self.recipients):
            raise NotificationsError(_("Two recipients have the same name."))
        return self

    def find(self, recipient_id: str) -> Recipient | None:
        return next((r for r in self.recipients if r.id == recipient_id), None)

    def subscribed_to(self, event: Event) -> list[Recipient]:
        """The recipients that are on and chose this event."""
        return [r for r in self.recipients if r.enabled and event in r.events]
