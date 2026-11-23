"""The permission report for auditors ("Berechtigungsbericht", OPS-19): page and download.

Every route needs the realm role `papaia-auditor` (`MANAGER_AUDITOR_ROLE`), checked here on
each request before anything else happens: without it the answer is 403 with no token
exchange and no call to papaia-audit, and the refusal is one `denied` row of
`papaia.audit/1` (`inventory_exported`), since papaia-audit never sees that retrieval.

With the role, papaia-audit builds the report for the auditor's own exchanged token
(`app.core.access_report`) and records the retrieval itself, so the manager writes no row
of its own for it and keeps no copy: the page shows the JSON answer, the download hands out
the bytes of the answer unchanged.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from papaia_kit.correlation import new_correlation_id
from papaia_kit.exchange import ClientSecret, ExchangeClient

from app.auth.csrf import get_csrf_token
from app.auth.deps import get_current_user
from app.auth.oidc import OIDCClaims
from app.auth.roles import is_admin, is_auditor
from app.auth.user_token import UserTokenUnavailable, user_access_token
from app.config import Settings, get_settings
from app.core import access_report, kit_audit
from app.core.audit import write_audit_entry
from app.i18n import gettext as _
from app.i18n import runtime as i18n_runtime
from app.shell import frame_language
from app.templating import templates as _templates

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]
SubjectToken = Callable[[Request], Awaitable[str]]
_PAGE = "/access-report"
_DOWNLOAD = "/access-report/download"
_ACTIONS = {"page": "access_report.view", "download": "access_report.download"}
_EXCHANGE_TIMEOUT = 10.0

_service: access_report.ReportService | None = None
_service_lock = threading.Lock()


def _build_service(settings: Settings) -> access_report.ReportService:
    # A client of its own: `forget` after each report then drops no other audience's token.
    exchange = ExchangeClient(
        token_endpoint=settings.oidc_issuer_kc_token,
        client_id=settings.manager_oidc_client_id,
        auth=ClientSecret(settings.manager_oidc_client_secret),
        timeout=_EXCHANGE_TIMEOUT,
    )
    post = access_report.mtls_post(
        ca_file=settings.audit_tls_ca_file,
        cert_file=settings.audit_tls_cert_file,
        key_file=settings.audit_tls_key_file,
    )
    return access_report.ReportService(
        exchange=exchange, post=post, base_url=settings.audit_read_url
    )


def get_report_service(settings: SettingsDep) -> access_report.ReportService:
    """The service that asks papaia-audit; built once per process (no token is kept in it)."""
    global _service  # noqa: PLW0603
    with _service_lock:
        if _service is None:
            _service = _build_service(settings)
        return _service


def get_subject_token() -> SubjectToken:
    """How the signed-in person's own access token is obtained (asked only after the role)."""
    return user_access_token


ServiceDep = Annotated[access_report.ReportService, Depends(get_report_service)]
SubjectDep = Annotated[SubjectToken, Depends(get_subject_token)]


def _who(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


def _require_auditor(user: OIDCClaims, settings: Settings, what: str) -> None:
    """403 unless the account holds the auditors' role; the refusal is an audit event."""
    if is_auditor(user, settings):
        return
    write_audit_entry(
        settings.papaia_config_dir,
        user=_who(user),
        action=_ACTIONS[what],
        target="access_report",
        result="denied",
        event="inventory_exported",
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=_("the %(role)s role is required") % {"role": settings.manager_auditor_role},
    )


def _query(request: Request, allowed: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """The query parameters, each known and at most once, each one of its allowed values."""
    values: dict[str, str] = {}
    for name, value in request.query_params.multi_items():
        if name not in allowed or name in values or value not in allowed[name]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_("invalid parameter: %(name)s") % {"name": name[:32]},
            )
        values[name] = value
    return values


def _correlation_id() -> str:
    return kit_audit.current_binding().correlation_id or new_correlation_id()


async def _subject(request: Request, subject_token: SubjectToken) -> str:
    try:
        return await subject_token(request)
    except UserTokenUnavailable:
        # The sign-in has ended: sign in again, as on every other page.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_("session expired"),
            headers={"Location": "/auth/login"},
        ) from None


async def _build(
    service: access_report.ReportService, token: str, by: str, fmt: str, cid: str
) -> access_report.Report:
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: service.build(token, by, fmt, cid)
    )


def _failure_text(failure: access_report.Unavailable) -> str:
    texts = {
        "exchange": _(
            "Your sign-in could not be passed on to the audit log service. Sign out and in "
            "again; if that does not help, the administration runs “papaia-ctl realm sync” "
            "on the host."
        ),
        "unreachable": _(
            "The audit log service does not answer. Check its state under "
            "“Monitor › Services” and reload the page afterwards."
        ),
        "status": _("The audit log service could not build the permission report."),
        "answer": _(
            "The audit log service answered with something that is not a permission report "
            "of the published format. Nothing of it is shown."
        ),
    }
    return texts[failure.reason]


def _titles() -> dict[str, Any]:
    return {
        "effect": {
            "r": _("read"), "w": _("write"), "d": _("delete"), "x": _("module interface"),
        },
        "kind": {"person": _("By person"), "system": _("By connected system")},
    }  # fmt: skip


@router.get(_PAGE, response_class=HTMLResponse)
async def access_report_page(
    request: Request,
    user: Annotated[OIDCClaims, Depends(get_current_user)],
    settings: SettingsDep,
    service: ServiceDep,
    subject_token: SubjectDep,
) -> HTMLResponse:
    _require_auditor(user, settings, "page")
    query = _query(request, {"by": access_report.KINDS, "due": ("", "1")})
    by, due_only = query.get("by", "person"), query.get("due") == "1"
    cid = _correlation_id()
    token = await _subject(request, subject_token)

    report: access_report.Report | None = None
    refusal: access_report.Refused | None = None
    failure: access_report.Unavailable | None = None
    code = status.HTTP_200_OK
    try:
        report = await _build(service, token, by, "json", cid)
    except access_report.Refused as exc:
        refusal, code = exc, status.HTTP_403_FORBIDDEN
    except access_report.Unavailable as exc:
        failure, code = exc, status.HTTP_502_BAD_GATEWAY
    del token

    doc = report.document if report is not None else None
    rows: list[dict[str, Any]] = doc["rows"] if doc is not None else []
    due = [row for row in rows if row["confirmation_due"]]
    return _templates.TemplateResponse(
        request,
        "access_report.html",
        {
            "request": request,
            "user": user,
            "csrf_token": get_csrf_token(request),
            # The kit's macros speak the frame's language (pk/components.html).
            "pk_lang": frame_language(i18n_runtime.locale_state().locale),
            "is_admin": is_admin(user, settings),
            "by": by,
            "due_only": due_only,
            "report": doc,
            "rows": due if due_only else rows,
            "total": len(rows),
            "due_count": len(due),
            "refusal": refusal,
            "failure": failure,
            "failure_text": _failure_text(failure) if failure is not None else None,
            "correlation_id": cid,
            "titles": _titles(),
            "target_rights": access_report.TARGET_RIGHTS,
        },
        status_code=code,
    )


@router.get(_DOWNLOAD)
async def access_report_download(
    request: Request,
    user: Annotated[OIDCClaims, Depends(get_current_user)],
    settings: SettingsDep,
    service: ServiceDep,
    subject_token: SubjectDep,
) -> Response:
    _require_auditor(user, settings, "download")
    query = _query(request, {"by": access_report.KINDS, "format": access_report.FORMATS})
    if set(query) != {"by", "format"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_("by and format are required"),
        )
    by, fmt = query["by"], query["format"]
    cid = _correlation_id()
    token = await _subject(request, subject_token)
    try:
        report = await _build(service, token, by, fmt, cid)
    except access_report.Refused as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"{exc.message} {exc.hint}"
        ) from None
    except access_report.Unavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_failure_text(exc)
        ) from None
    finally:
        del token
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return Response(
        content=report.body,
        media_type=report.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="access-report-{by}-{stamp}.{fmt}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
