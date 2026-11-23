"""The inventory of work objects ("Bestand", governance GV-21): page, list and export.

Every route needs the realm role `papaia-auditor` (`MANAGER_AUDITOR_ROLE`), checked here on
each request; the navigation entry is presentation only. The list is collected from the
modules on every request (`app.core.work_inventory`) and reading it writes nothing. Each
export, a refused one too, is one entry in the audit log and one `inventory_exported` row of
`papaia.audit/1` with the checksum of what was handed out; no object, title or person is
written into the row.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse

from app.auth.csrf import get_csrf_token
from app.auth.deps import AuditorUser, get_current_user
from app.auth.oidc import OIDCClaims
from app.auth.roles import is_admin, is_auditor
from app.config import Settings, get_settings
from app.core import work_inventory
from app.core.audit import csv_safe, write_audit_entry
from app.core.module_catalog import localized, read_catalog
from app.i18n import gettext as _
from app.i18n import runtime as i18n_runtime
from app.shell import frame_language
from app.templating import templates as _templates

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]
_EXPORT_FORMATS = ("csv", "json")
_ACTION = "inventory.export"
_TITLES = {
    "studio": {"de": "Studio", "en": "Studio"},
    "flow": {"de": "Flow", "en": "Flow"},
    "data": {"de": "Data", "en": "Data"},
    "assist": {"de": "Assistant", "en": "Assistant"},
}


def get_inventory_fetch(settings: SettingsDep) -> work_inventory.Fetch:
    """How the modules are asked: mutual TLS with the manager's platform client certificate."""
    return work_inventory.mtls_fetch(
        ca_file=settings.audit_tls_ca_file,
        cert_file=settings.audit_tls_cert_file,
        key_file=settings.audit_tls_key_file,
    )


FetchDep = Annotated[work_inventory.Fetch, Depends(get_inventory_fetch)]


def _filters(request: Request, *, extra: tuple[str, ...] = ()) -> work_inventory.Filters:
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k not in extra]
    try:
        return work_inventory.Filters.parse(pairs)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_("invalid filter: %(name)s") % {"name": str(exc)[:32]},
        ) from None


def _installed(settings: Settings) -> list[str]:
    """Modules of the bill of materials the platform catalog shows as installed."""
    view = read_catalog(settings.papaia_workspace_dir, settings.papaia_config_dir)
    return [
        m.name.removeprefix("papaia-")
        for m in view.modules
        if m.state != "available" and m.name.removeprefix("papaia-") in _TITLES
    ]


async def _collect(settings: Settings, fetch: work_inventory.Fetch) -> dict[str, Any]:
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: work_inventory.collect(
            settings.inventory_source_map, fetch, also_ask=_installed(settings)
        ),
    )


@cache
def _data_classes() -> dict[str, dict[str, str]]:
    raw = (
        resources.files("papaia_kit")
        .joinpath("_data/schemas/data-classes.json")
        .read_text(encoding="utf-8")
    )
    return {c["id"]: dict(c["title"]) for c in json.loads(raw)["classes"]}


def _title(module: str) -> str:
    return localized(_TITLES.get(module, {"en": module}))


def _titles() -> dict[str, dict[str, str]]:
    """What the page shows for the closed value sets of the contract, in the page's language."""
    return {
        "kind": {
            "app": _("App"), "workflow": _("Workflow"), "metric": _("Metric"),
            "dashboard": _("Dashboard"), "event_rule": _("Event rule"),
            "event_reminder": _("Event reminder"),
        },
        "flag": {
            "owner_inactive": _("Owner no longer active"), "deputy_missing": _("Deputy missing"),
            "unused_180d": _("Unused for 180 days"), "confirmation_due": _("Confirmation due"),
            "class_above_role": _("Class above the role"),
        },
        "effect": {
            "read": _("read"), "write": _("write"), "send": _("send"), "delete": _("delete"),
            "check": _("check"),
        },
        "module": {m: _title(m) for m in sorted(_TITLES)},
        "data_class": {k: localized(v) for k, v in _data_classes().items()},
    }  # fmt: skip


def _options(titles: dict[str, dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """The choices of each filter for the kit's select field: "All" first, then the set."""
    sets: dict[str, tuple[str, ...]] = {
        "flag": work_inventory.FLAGS,
        "module": tuple(sorted(_TITLES)),
        "data_class": work_inventory.DATA_CLASSES,
        "effect": work_inventory.EFFECTS,
    }
    return {
        name: [{"value": "", "label": _("All")}]
        + [{"value": v, "label": titles[name].get(v, v)} for v in values]
        for name, values in sets.items()
    }


def _who(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


@router.get("/inventory", response_class=HTMLResponse)
async def inventory_page(
    request: Request, user: AuditorUser, settings: SettingsDep, fetch: FetchDep
) -> HTMLResponse:
    filters = _filters(request)
    doc = await _collect(settings, fetch)
    shown = work_inventory.narrowed(doc, filters)
    query = request.url.query
    titles = _titles()
    return _templates.TemplateResponse(
        request,
        "inventory.html",
        {
            "request": request,
            "user": user,
            "csrf_token": get_csrf_token(request),
            # The kit's macros speak the frame's language (pk/components.html).
            "pk_lang": frame_language(i18n_runtime.locale_state().locale),
            "is_admin": is_admin(user, settings),
            "items": [
                {**item, "effects": work_inventory.effects(item)} for item in shown["items"]
            ],
            "total": len(doc["items"]),
            "unreachable": [_title(m) for m in work_inventory.unreachable(doc)],
            "filters": filters.as_dict(),
            "titles": titles,
            "options": _options(titles),
            "export_query": ("&" + query) if query else "",
            "checksum": shown["checksum"],
        },
    )


@router.get("/api/v1/inventory")
async def inventory_list(
    request: Request, user: AuditorUser, settings: SettingsDep, fetch: FetchDep
) -> dict[str, Any]:
    filters = _filters(request)
    return work_inventory.narrowed(await _collect(settings, fetch), filters)


@router.get("/api/v1/inventory/export")
async def inventory_export(
    request: Request,
    user: Annotated[OIDCClaims, Depends(get_current_user)],
    settings: SettingsDep,
    fetch: FetchDep,
) -> Response:
    if not is_auditor(user, settings):
        # A refused export is an event of its own (OPS-19, PLT-60-A4).
        write_audit_entry(
            settings.papaia_config_dir,
            user=_who(user),
            action=_ACTION,
            target="inventory",
            result="denied",
            event="inventory_exported",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_("the %(role)s role is required") % {"role": settings.manager_auditor_role},
        )
    formats = request.query_params.getlist("format")
    if len(formats) != 1 or formats[0] not in _EXPORT_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=_("format must be csv or json")
        )
    fmt = formats[0]
    filters = _filters(request, extra=("format",))
    doc = work_inventory.narrowed(await _collect(settings, fetch), filters)

    if fmt == "json":
        body = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        media_type = "application/json"
    else:
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=work_inventory.CSV_COLUMNS, lineterminator="\r\n")
        writer.writeheader()
        for item in doc["items"]:
            cells = work_inventory.csv_cells(item)
            writer.writerow({k: csv_safe(v) for k, v in cells.items()})
        for module in work_inventory.unreachable(doc):
            writer.writerow({"module": module, "state": work_inventory.UNREACHABLE})
        body = out.getvalue()
        media_type = "text/csv"

    write_audit_entry(
        settings.papaia_config_dir,
        user=_who(user),
        action=_ACTION,
        target="inventory",
        params={
            "format": fmt,
            "items": len(doc["items"]),
            "filters": filters.as_dict(),
            "unreachable": work_inventory.unreachable(doc),
        },
        event="inventory_exported",
        object_ref={"type": "inventory", "id": doc["checksum"]},
    )
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="inventory-{stamp}.{fmt}"',
            "Cache-Control": "no-store",
        },
    )
