"""Manager settings, stored in `$PAPAIA_CONFIG_DIR/manager/settings.yaml`.

One document with one top-level section per topic (`branding` and `host` today,
further sections such as `smtp` later). Every section has defaults for all of its
fields, so a missing file, a missing section or a section written by a newer
release never stops the manager from rendering: unknown keys are ignored on
read and dropped on the next save.

The uploaded logo lives next to the document in `manager/branding/`.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

DEFAULT_NAME = "papAIa manager"
DEFAULT_TAGLINE = "by Fidonis"

MAX_NAME_LENGTH = 40
MAX_TAGLINE_LENGTH = 60
MAX_LOGO_BYTES = 512 * 1024

# How often the host is measured again. The floor keeps a run of `doctor` (a
# Python start-up and a handful of probes) from overlapping the next one, and the
# ceiling keeps "every few hours" from reading as a hung page.
MIN_REFRESH_SECONDS = 10
MAX_REFRESH_SECONDS = 3600
DEFAULT_REFRESH_SECONDS = 60

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Magic bytes decide the type, never the client's filename or Content-Type.
_LOGO_TYPES: dict[str, tuple[str, tuple[bytes, ...]]] = {
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "webp": ("image/webp", ()),  # RIFF....WEBP, checked separately
    "svg": ("image/svg+xml", ()),  # text, checked separately
}

# What makes an SVG executable or able to pull in other resources. The file is
# only ever served for an <img>, where none of this runs, but the same bytes
# could be opened directly by URL, so refuse them at the door.
_SVG_FORBIDDEN = re.compile(
    rb"<\s*script|<\s*foreignObject|\son[a-z]+\s*=|javascript:|<\s*!ENTITY|<\s*iframe",
    re.IGNORECASE,
)


class SettingsError(ValueError):
    """A submitted value that cannot be stored."""


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    return _CONTROL_CHARS.sub("", value).strip()


class BrandingSettings(BaseModel):
    """Sidebar branding.

    `None` means "use the built-in default"; an empty tagline is a deliberate
    "show no second line" and is kept apart from `None`.
    """

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    tagline: str | None = Field(default=None, max_length=MAX_TAGLINE_LENGTH)
    logo: str | None = None


class HostSettings(BaseModel):
    """Host monitoring.

    The value read from the file is clamped, not rejected. A pydantic error here
    would send `load_settings` back to the defaults for the whole document and
    take the branding with it, over a number somebody edited by hand. The API is
    the strict side: `validate_refresh_seconds` refuses what the file only clamps.
    """

    model_config = ConfigDict(extra="ignore")

    refresh_seconds: int = DEFAULT_REFRESH_SECONDS

    @field_validator("refresh_seconds", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> int:
        if isinstance(value, bool):
            return DEFAULT_REFRESH_SECONDS
        try:
            seconds = int(value)
        except (TypeError, ValueError, OverflowError):
            return DEFAULT_REFRESH_SECONDS
        return min(max(seconds, MIN_REFRESH_SECONDS), MAX_REFRESH_SECONDS)


class ManagerSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    branding: BrandingSettings = Field(default_factory=BrandingSettings)
    host: HostSettings = Field(default_factory=HostSettings)


@dataclass(frozen=True)
class EffectiveBranding:
    name: str
    tagline: str
    logo_url: str | None


def _settings_path(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / "settings.yaml"


def _branding_dir(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / "branding"


def load_settings(config_dir: str) -> ManagerSettings:
    """Read the document; anything unreadable yields the defaults."""
    path = _settings_path(config_dir)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ManagerSettings()
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("settings.yaml unreadable, using defaults: %s", exc)
        return ManagerSettings()
    if not isinstance(raw, dict):
        return ManagerSettings()
    try:
        return ManagerSettings.model_validate(raw)
    except ValidationError as exc:
        logger.warning("settings.yaml invalid, using defaults: %s", exc)
        return ManagerSettings()


def save_settings(config_dir: str, settings: ManagerSettings) -> None:
    """Atomically write settings.yaml."""
    path = _settings_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        "# papaia-manager settings. Edited through the Settings page.\n"
        + yaml.dump(
            settings.model_dump(), default_flow_style=False, sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def settings_revision(config_dir: str) -> str:
    """Content hash of settings.yaml, or "" when it does not exist."""
    path = _settings_path(config_dir)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def validate_branding(name: str | None, tagline: str | None) -> tuple[str | None, str | None]:
    """Normalise submitted text. Blank name -> default; blank tagline -> hidden."""
    cleaned_name = _clean(name)
    cleaned_tagline = _clean(tagline)
    if cleaned_name is not None and len(cleaned_name) > MAX_NAME_LENGTH:
        raise SettingsError(f"Name must be at most {MAX_NAME_LENGTH} characters.")
    if cleaned_tagline is not None and len(cleaned_tagline) > MAX_TAGLINE_LENGTH:
        raise SettingsError(f"Tagline must be at most {MAX_TAGLINE_LENGTH} characters.")
    return (cleaned_name or None), cleaned_tagline


def validate_refresh_seconds(value: int) -> int:
    """The interval as submitted, or `SettingsError` when it is out of range."""
    if not MIN_REFRESH_SECONDS <= value <= MAX_REFRESH_SECONDS:
        raise SettingsError(
            f"The refresh interval must be between {MIN_REFRESH_SECONDS} seconds"
            f" and {MAX_REFRESH_SECONDS // 60} minutes."
        )
    return value


def refresh_interval(config_dir: str) -> int:
    """Seconds between two measurements of the host. Never raises."""
    return load_settings(config_dir).host.refresh_seconds


def format_interval(seconds: int) -> str:
    """`30 s`, `60 s`, `5 min`, `90 s`: whole minutes above one are spelled as minutes."""
    return f"{seconds // 60} min" if seconds > 60 and seconds % 60 == 0 else f"{seconds} s"


def logo_path(config_dir: str, settings: ManagerSettings | None = None) -> Path | None:
    """Path of the stored logo, or None when none is set or the file is gone."""
    current = settings or load_settings(config_dir)
    filename = current.branding.logo
    if not filename or filename != Path(filename).name:
        return None
    path = _branding_dir(config_dir) / filename
    return path if path.is_file() else None


def logo_media_type(path: Path) -> str:
    for ext, (media_type, _) in _LOGO_TYPES.items():
        if path.suffix.lower() == f".{ext}":
            return media_type
    return "application/octet-stream"


def detect_logo_type(data: bytes) -> str:
    """Return the extension for supported image bytes, else raise."""
    if not data:
        raise SettingsError("The file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise SettingsError(f"The logo must be smaller than {MAX_LOGO_BYTES // 1024} KB.")
    if data.startswith(_LOGO_TYPES["png"][1]):
        return "png"
    if data.startswith(_LOGO_TYPES["jpg"][1]):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in data[:4096].lower():
        if _SVG_FORBIDDEN.search(data):
            raise SettingsError("The SVG contains scripts or external content and was refused.")
        return "svg"
    raise SettingsError("Unsupported logo format. Use PNG, JPEG, WebP or SVG.")


def save_logo(config_dir: str, data: bytes) -> str:
    """Store a logo, replacing any previous one. Returns the stored filename."""
    ext = detect_logo_type(data)
    directory = _branding_dir(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # Content hash in the name: a new upload is a new URL, so no cache has to
    # be told to let go of the old one.
    filename = f"logo-{hashlib.sha256(data).hexdigest()[:12]}.{ext}"
    target = directory / filename
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    remove_logo_files(config_dir, keep=filename)
    return filename


def remove_logo_files(config_dir: str, keep: str | None = None) -> None:
    directory = _branding_dir(config_dir)
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.name != keep and entry.name.startswith("logo-"):
            try:
                entry.unlink()
            except OSError as exc:
                logger.warning("could not remove old logo %s: %s", entry, exc)


def effective_branding(config_dir: str) -> EffectiveBranding:
    """Resolve defaults. Never raises: the sidebar must always render."""
    try:
        settings = load_settings(config_dir)
        branding = settings.branding
        has_logo = logo_path(config_dir, settings) is not None
        return EffectiveBranding(
            name=branding.name or DEFAULT_NAME,
            tagline=DEFAULT_TAGLINE if branding.tagline is None else branding.tagline,
            logo_url=f"/brand/logo?v={branding.logo}" if has_logo else None,
        )
    except Exception:  # noqa: BLE001 - presentation must not fail on bad config
        logger.exception("could not resolve branding, using defaults")
        return EffectiveBranding(DEFAULT_NAME, DEFAULT_TAGLINE, None)


def settings_to_json(settings: ManagerSettings) -> dict[str, Any]:
    return settings.model_dump()
