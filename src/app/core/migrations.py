"""One-time moves of the manager's own data when a release changes where a value lives.

The files the manager keeps under `$PAPAIA_CONFIG_DIR/manager/` are read by a newer release as
an older one wrote them (`tests/test_upgrade_from_1_6_0.py` holds 1.8.0 to the files of 1.6.0),
so most releases need nothing here. A value that moves to another owner does: its migration
runs in the background when the manager starts, and `manager/migrations.json` (mode 0600)
records each one that is done, with what became of every field, so it runs once.

A migration that cannot finish now (the core it hands a value to is not there yet, or does not
answer) is not recorded and runs again at the next start. A value the core refuses is recorded
as `refused` and not offered again: the operator sets it on the page instead. Each run that
asks the core to write leaves one entry in the audit log and one `papaia.audit/1` row, as the
manager's service identity, naming the fields and what became of them, never their values.

Migrations of 1.8.0:

- `1.8.0-branding`: the sidebar brand of manager 1.7.0 and earlier (`manager/settings.yaml`,
  `manager/branding/`) is shown by 1.8.0 only while the core publishes no platform catalog.
  From papAIa core 1.10.0 on, every module shows the brand of the catalog, which the core keeps
  (Settings > Installation, `app.core.installation`). The old name and second line become the
  installation's name and suffix and the old logo its logo, each only where the core has no value
  of its own yet; an SVG logo, which the core does not take, stays with the manager. The old
  values are left in place, so a manager rolled back to 1.7.0 still finds them.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from papaia_kit.correlation import new_correlation_id

from app.core import installation, kit_audit, settings_store
from app.core.audit import write_audit_entry
from app.core.module_catalog import platform_catalog

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

LEDGER_NAME = "migrations.json"
LEDGER_FORMAT = "papaia-manager.migrations/1"
MAX_LEDGER_BYTES = 64 * 1024
# The name the core's catalog carries while no operator has named the installation
# (`DEFAULT_BRAND_NAME` of the core's catalog module).
CORE_DEFAULT_NAME = "papAIa"

# What became of one field.
APPLIED = "applied"  # handed to the core
KEPT = "kept"  # the core has a value of its own, which stays
REFUSED = "refused"  # the core, or the manager's check of the core's rules, refused it
UNSUPPORTED = "unsupported"  # the core does not take this kind of value (an SVG logo)

_WRITES = frozenset({APPLIED, REFUSED})


class LedgerError(Exception):
    """`manager/migrations.json` exists but cannot be used; no migration runs then."""


@dataclass(frozen=True)
class Outcome:
    """One migration after a run: `done` once recorded, `fields` what became of each value."""

    id: str
    done: bool
    fields: dict[str, str] = field(default_factory=dict)


class _NotNowError(Exception):
    """The migration cannot finish now; it runs again at the next start."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__()
        self.fields = fields


# -- the ledger ------------------------------------------------------------------------------


def ledger_path(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / LEDGER_NAME


def load_ledger(config_dir: str) -> dict[str, dict[str, Any]]:
    """The recorded migrations by id; empty without a ledger. Raises LedgerError."""
    path = ledger_path(config_dir)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_LEDGER_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise LedgerError(f"{LEDGER_NAME} cannot be read ({type(exc).__name__})") from exc
    if len(raw) > MAX_LEDGER_BYTES:
        raise LedgerError(f"{LEDGER_NAME} is larger than {MAX_LEDGER_BYTES} bytes")
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise LedgerError(f"{LEDGER_NAME} is not JSON") from exc
    if not isinstance(doc, dict) or doc.get("format") != LEDGER_FORMAT:
        raise LedgerError(f"{LEDGER_NAME} is not a {LEDGER_FORMAT} document")
    entries = doc.get("migrations")
    if not isinstance(entries, dict) or not all(
        isinstance(key, str) and isinstance(value, dict) for key, value in entries.items()
    ):
        raise LedgerError(f"{LEDGER_NAME} has no valid list of migrations")
    return entries


def _save_ledger(config_dir: str, entries: dict[str, dict[str, Any]]) -> None:
    path = ledger_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"format": LEDGER_FORMAT, "migrations": entries}, indent=2,
                      sort_keys=True).encode()
    fd, name = tempfile.mkstemp(prefix=".migrations-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data + b"\n")
        os.chmod(name, 0o600)
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
        raise


# -- 1.8.0-branding --------------------------------------------------------------------------


def _legacy_brand(config_dir: str) -> tuple[list[tuple[str, str]], bytes | None]:
    """The old brand's texts as core settings, and the old logo's bytes."""
    stored = settings_store.load_settings(config_dir)
    texts: list[tuple[str, str]] = []
    if stored.branding.name:
        texts.append(("branding.name", stored.branding.name))
    # None is the default second line ("by Fidonis"), "" a deliberately hidden one: neither is
    # a suffix of the installation.
    if stored.branding.tagline:
        texts.append(("branding.suffix", stored.branding.tagline))
    logo = settings_store.logo_path(config_dir, stored)
    data: bytes | None = None
    if logo is not None:
        with logo.open("rb") as handle:
            data = handle.read(installation.LOGO_MAX_BYTES + 1)
    return texts, data


async def _branding(settings: Settings) -> dict[str, str]:
    config_dir = settings.papaia_config_dir
    texts, logo = _legacy_brand(config_dir)
    if not texts and logo is None:
        return {}
    # A core before 1.10.0 publishes no catalog and has no settings commands; there the manager
    # still shows the old brand itself, so waiting loses nothing.
    if platform_catalog(config_dir) is None:
        raise _NotNowError({})
    paths = installation.Paths(settings.papaia_workspace_dir, config_dir)
    fields: dict[str, str] = {}
    try:
        current = (await installation.show(paths))["branding"]
    except installation.InstallationError as exc:
        logger.warning("migration 1.8.0-branding: the core did not answer: %s", exc)
        raise _NotNowError(fields) from exc
    for key, value in texts:
        own = current.get("name") if key == "branding.name" else current.get("suffix")
        if own and not (key == "branding.name" and own == CORE_DEFAULT_NAME):
            fields[key] = KEPT
            continue
        fields[key] = await _apply(paths, fields, key, value)
    if logo is not None:
        if current.get("logo"):
            fields["branding.logo"] = KEPT
        elif installation.logo_problem(logo) is not None:
            fields["branding.logo"] = UNSUPPORTED
        else:
            fields["branding.logo"] = await _apply_logo(paths, fields, logo)
    return fields


async def _apply(
    paths: installation.Paths, fields: dict[str, str], key: str, value: str
) -> str:
    try:
        checked = installation.check_text(key, value)
    except installation.InstallationError:
        return REFUSED
    return await _outcome(fields, installation.apply(paths, [(key, checked)]))


async def _apply_logo(paths: installation.Paths, fields: dict[str, str], data: bytes) -> str:
    return await _outcome(fields, installation.set_logo(paths, data))


async def _outcome(fields: dict[str, str], change: Awaitable[Any]) -> str:
    try:
        await change
    except installation.InstallationError as exc:
        if exc.status in (409, 422):
            return REFUSED
        logger.warning("migration 1.8.0-branding: the core did not apply a value: %s", exc)
        raise _NotNowError(fields) from exc
    return APPLIED


# -- running them ----------------------------------------------------------------------------

MIGRATIONS: tuple[tuple[str, Callable[[Settings], Awaitable[dict[str, str]]]], ...] = (
    ("1.8.0-branding", _branding),
)


def _audit(config_dir: str, migration: str, fields: dict[str, str], *, failed: bool) -> None:
    if not failed and not _WRITES.intersection(fields.values()):
        return
    result = "failed" if failed else ("refused" if REFUSED in fields.values() else "ok")
    topic = migration.split("-", 1)[1]
    write_audit_entry(
        config_dir,
        user=kit_audit.SOURCE,
        action=f"migration.{topic}",
        target=f"installation:{topic}",
        params={"migration": migration, "fields": dict(fields)},
        result=result,
        event="config_changed",
        object_ref={"type": "setting", "id": topic},
    )


async def run_pending(settings: Settings) -> list[Outcome]:
    """Run every migration the ledger does not list yet; returns one outcome per migration.

    Returns nothing, and changes nothing, while the ledger cannot be read: a damaged ledger
    must not make a migration run twice."""
    config_dir = settings.papaia_config_dir
    try:
        ledger = load_ledger(config_dir)
    except LedgerError as exc:
        logger.error("manager migrations not run: %s", exc)
        return []
    outcomes: list[Outcome] = []
    for migration, run in MIGRATIONS:
        recorded = ledger.get(migration)
        if recorded is not None:
            outcomes.append(Outcome(migration, True, dict(recorded.get("fields") or {})))
            continue
        with kit_audit.bound(kit_audit.SERVICE, new_correlation_id()):
            try:
                fields = await run(settings)
            except _NotNowError as pending:
                _audit(config_dir, migration, pending.fields,
                       failed=bool(pending.fields) or pending.__cause__ is not None)
                outcomes.append(Outcome(migration, False, pending.fields))
                continue
            _audit(config_dir, migration, fields, failed=False)
        ledger[migration] = {
            "done_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fields": fields,
        }
        _save_ledger(config_dir, ledger)
        logger.info("manager migration %s done: %s", migration, fields or "nothing to move")
        outcomes.append(Outcome(migration, True, fields))
    return outcomes


async def run_at_start(settings: Settings) -> None:
    """`run_pending` for the start of the manager: never raises, a failure costs the move."""
    try:
        await run_pending(settings)
    except Exception:
        logger.exception("manager migrations failed; they run again at the next start")
