"""Write the 1.6.0 data set of `test_upgrade_from_1_6_0.py` with the 1.6.0 code itself.

    git archive v1.6.0 src | tar -x -C <dir>
    cd src && uv run --frozen python ../tests/fixtures/make_manager_1_6_0.py <dir>/src <out>

`<out>/manager/` then holds what manager 1.6.0 writes; copy it to `tests/fixtures/manager-1.6.0/`.
Every value is made up. The timestamps the code takes from the clock and the recipient's random
id are set to fixed values afterwards, so a new run gives the same files.
"""
from __future__ import annotations

import re
import struct
import sys
import zlib
from datetime import UTC, datetime
from pathlib import Path


def _png(width: int = 2, height: int = 2) -> bytes:
    raw = b"".join(b"\x00" + b"\x10\x80\xc0\xff" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def main(source: str, out: str) -> None:
    sys.path.insert(0, source)
    from app.core import (  # noqa: PLC0415 - the 1.6.0 package, from `source`
        audit,
        catalogs,
        schedule,
        settings_store,
        snapshots,
        tiles,
        user_prefs,
    )
    from app.core.config import history  # noqa: PLC0415
    from app.core.notifications import store  # noqa: PLC0415
    from app.core.notifications.model import Event, EventSettings, Kind  # noqa: PLC0415

    cfg = out
    (Path(cfg) / "manager").mkdir(parents=True, exist_ok=True)

    stored = settings_store.load_settings(cfg)
    stored.branding.name, stored.branding.tagline = settings_store.validate_branding(
        "Beispiel GmbH", "IT-Betrieb"
    )
    stored.branding.logo = settings_store.save_logo(cfg, _png())
    stored.host.refresh_seconds = 120
    settings_store.save_settings(cfg, stored)

    schedule.save_schedule(cfg, schedule.build_schedule(
        enabled=True, cron="30 2 * * *", timezone="Europe/Berlin", retention_days=14,
    ))

    tiles.save_tiles(cfg, tiles.TilesConfig(groups=[
        tiles.TileGroup(name="Arbeitsplatz", tiles=[
            tiles.Tile(name="Wiki", href="https://wiki.example.org", description="Handbuch"),
            tiles.WidgetTile(widget="core_version", size="s"),
        ]),
    ]))

    registry = catalogs.load_registry(cfg)
    registry.catalogs.append(
        catalogs.Catalog(name="haus", type="local", path="/srv/papaia/catalog-haus")
    )
    catalogs.save_registry(cfg, registry)

    snapshots.record_installed(
        cfg, name="paperless", catalog="fidonis",
        commit="0123456789abcdef0123456789abcdef01234567", manifest_version="2.0.0",
        history=[snapshots.HistoryEntry(
            commit="89abcdef0123456789abcdef0123456789abcdef", manifest_version="1.9.0",
            installed_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        )],
    )

    user_prefs.set_locale(cfg, "22222222-2222-2222-2222-222222222222", "de")

    loaded = store.load_config(cfg)
    loaded, _recipient = store.add_recipient(
        cfg, loaded.revision, name="Betrieb", kind=Kind.EMAIL, target="betrieb@example.org",
        enabled=True, events=[Event.JOB_FAILED, Event.CERT_EXPIRING],
    )
    store.save_settings(cfg, loaded.revision, EventSettings(
        check_interval_minutes=10, debounce_minutes=60, cert_lead_days=21,
    ))

    history.save(cfg, "litellm", b"model_list: []\n", "apply",
                 now=datetime(2026, 11, 1, 9, 0, tzinfo=UTC))

    audit.write_audit_entry(cfg, user="anna", action="settings.branding.update",
                            target="settings", params={"name": "Beispiel GmbH"})
    audit.write_audit_entry(cfg, user="anna", action="addon.install", target="paperless",
                            job_id="33333333-3333-3333-3333-333333333333")

    _fix_clock_and_ids(Path(cfg) / "manager")


def _fix_clock_and_ids(folder: Path) -> None:
    notifications = folder / "notifications.yaml"
    notifications.write_text(
        re.sub(r"(?m)^- id: [0-9a-f]{8}$", "- id: betrieb",
               notifications.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    log = folder / "audit.log"
    stamps = iter(("2026-11-05T09:12:00+00:00", "2026-11-05T09:20:00+00:00"))
    log.write_text(
        re.sub(r'"ts": "[^"]+"', lambda _m: f'"ts": "{next(stamps)}"',
               log.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    installed = folder / "installed.yaml"
    installed.write_text(
        re.sub(r"(?m)^(    installed_at: )'[^']+'$", r"\1'2026-11-05T09:25:00+00:00'",
               installed.read_text(encoding="utf-8")),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
