"""The page "Data subject requests" (Betroffenenanträge): confirm requests for information
and erasure of a person without an account.

The operator files a request on the host (`papaia-ctl person export|erase`) and names a second
person; that person opens this page, signs in again and confirms or rejects it
(security 1.14 no. 2b, OPS-08). Nothing is kept here: the list is read from papaia-audit on
every visit (`app.core.person_requests`) with the signed-in person's own token.

Rules, each checked on the server on every request:

- every route needs the confirmation role or the platform's administration role
  (`require_confirmer`); papaia-audit checks the role again;
- a confirmation needs a sign-in of at most five minutes ago (`stepped_up`), and papaia-audit
  checks the token's `auth_time` again;
- the person who filed a request cannot confirm it: refused here from papaia-audit's own
  answer (`own`) before any decision is sent, and refused by papaia-audit and its database;
- the forms carry the session's CSRF token and nothing else;
- every decision, a refused one too, is one entry in the audit log and one `papaia.audit/1`
  row with the request as `object_ref` and the request's correlation id, which papaia-audit
  receives as well. Never the passphrase.

Confirming an export answers the passphrase of the archive. The page that answers the
confirmation shows it once, with `Cache-Control: no-store`; it is not stored, not logged and
not part of any later page. Reloading that page sends the confirmation again, which
papaia-audit refuses because the request is no longer open.
"""
from __future__ import annotations

from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from papaia_kit.correlation import new_correlation_id

from app.auth.csrf import get_csrf_token, verify_csrf
from app.auth.deps import ConfirmerUser, stepped_up
from app.auth.oidc import OIDCClaims
from app.auth.user_token import UserTokenUnavailable, user_access_token
from app.config import Settings, get_settings
from app.core import kit_audit
from app.core import person_requests as pr
from app.core.audit import write_audit_entry
from app.i18n import gettext as _
from app.i18n import runtime as i18n_runtime
from app.routers.ui import _ctx, _no_store
from app.shell import frame_language
from app.templating import templates as _templates

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]
PAGE = "/person-requests"
_FORM_FIELDS = frozenset({"pk_csrf"})
_MAX_FORM_VALUE = 512

# Status of the page and result of the audit entry for each refusal.
_REFUSALS: dict[pr.Reason, tuple[int, str]] = {
    "not_fresh": (status.HTTP_403_FORBIDDEN, "refused"),
    "own_request": (status.HTTP_403_FORBIDDEN, "denied"),
    "no_role": (status.HTTP_403_FORBIDDEN, "denied"),
    "not_found": (status.HTTP_404_NOT_FOUND, "not_found"),
    "wrong_state": (status.HTTP_409_CONFLICT, "conflict"),
    "unauthenticated": (status.HTTP_403_FORBIDDEN, "refused"),
    "unavailable": (status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable"),
}


# Refusals the person can act on are warnings; the rest are errors.
_WARNINGS = frozenset({"not_fresh", "own_request", "wrong_state"})


def _messages() -> dict[str, str]:
    """What the page says for each refusal, in the page's language."""
    return {
        "not_fresh": _(
            "Confirming needs a sign-in of at most five minutes ago. Confirm your sign-in and "
            "confirm the request again."
        ),
        "own_request": _(
            "You filed this request yourself and cannot confirm it. The second person named "
            "in the request confirms it."
        ),
        "no_role": _("Only persons with the confirmation role decide on these requests."),
        "not_found": _("This request does not exist or does not name you."),
        "wrong_state": _(
            "The request is no longer open: it was decided already or its time ran out. "
            "Reload the list."
        ),
        "unauthenticated": _("Your sign-in could not be passed on. Sign in again and retry."),
        "unavailable": _(
            "The service that keeps the requests does not answer. The list cannot be shown "
            "and nothing was decided. Try again later; if it persists, check the state under "
            "“Services”."
        ),
    }


async def person_token(request: Request) -> str:
    """The signed-in person's access token; without one the browser signs in again."""
    try:
        return await user_access_token(request)
    except UserTokenUnavailable:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_("session expired"),
        ) from None


TokenDep = Annotated[str, Depends(person_token)]


def _cid() -> str:
    return kit_audit.current_binding().correlation_id or new_correlation_id()


def _titles() -> dict[str, dict[str, str]]:
    return {
        "operation": {"export": _("Information"), "erase": _("Erasure")},
        "state": {
            "open": _("open"),
            "confirmed": _("confirmed"),
            "rejected": _("rejected"),
            "running": _("running"),
            "done": _("done"),
            "partial": _("partial"),
            "expired": _("expired"),
        },
    }


async def _list(settings: Settings, token: str, cid: str) -> list[pr.PersonRequest]:
    return await anyio.to_thread.run_sync(lambda: pr.list_requests(settings, token, cid))


def _page(
    request: Request,
    user: OIDCClaims,
    items: list[pr.PersonRequest] | None,
    *,
    status_code: int = status.HTTP_200_OK,
    error: str | None = None,
    decided: pr.Decided | None = None,
) -> HTMLResponse:
    context: dict[str, Any] = {
        "items": items or [],
        "listed": items is not None,
        "error": _messages()[error] if error else None,
        "error_reason": error,
        "error_tone": "warning" if error in _WARNINGS else "error",
        "state_tones": {state: "warning" if state == "open" else "neutral" for state in pr.STATES},
        "decided": decided,
        "titles": _titles(),
        "stepped_up": stepped_up(request),
        "step_up_href": f"/auth/step-up?next={PAGE}",
        # The kit's macros speak the frame's language and sign their forms (pk/components.html).
        "pk_lang": frame_language(i18n_runtime.locale_state().locale),
        "pk_csrf": get_csrf_token(request),
    }
    return _no_store(
        _templates.TemplateResponse(
            request,
            "person_requests.html",
            _ctx(request, user, **context),
            status_code=status_code,
        )
    )


@router.get(PAGE, response_class=HTMLResponse)
async def person_requests_page(
    request: Request, user: ConfirmerUser, settings: SettingsDep, token: TokenDep
) -> HTMLResponse:
    """The requests of the last 7 days that name the signed-in person; reading writes nothing."""
    try:
        items = await _list(settings, token, _cid())
    except pr.PersonRequestError as exc:
        return _page(request, user, None, status_code=_REFUSALS[exc.reason][0], error=exc.reason)
    return _page(request, user, items)


async def _form(request: Request) -> None:
    """The decision forms carry the session's CSRF token and nothing else."""
    form = await request.form()
    token = form.get("pk_csrf")
    verify_csrf(request, token if isinstance(token, str) else None)
    if set(form.keys()) - _FORM_FIELDS or any(
        not isinstance(v, str) or len(v) > _MAX_FORM_VALUE for _k, v in form.multi_items()
    ) or len(form.getlist("pk_csrf")) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_("Invalid value.")
        )


def _audit(
    settings: Settings,
    user: OIDCClaims,
    decision: pr.Decision,
    request_id: str,
    *,
    result: str = "ok",
    operation: str | None = None,
    code: str | None = None,
) -> None:
    params: dict[str, Any] = {}
    if operation is not None:
        params["operation"] = operation
    if code is not None:
        params["code"] = code
    write_audit_entry(
        settings.papaia_config_dir,
        user=user.preferred_username,
        action=f"person_request.{decision}",
        target=request_id,
        params=params or None,
        result=result,
        object_ref={"type": "person_request", "id": request_id},
    )


async def _decide(
    request: Request,
    request_id: str,
    decision: pr.Decision,
    user: OIDCClaims,
    settings: Settings,
    token: str,
) -> HTMLResponse:
    if not pr.ID.fullmatch(request_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await _form(request)
    cid = _cid()

    def refused(reason: pr.Reason, items: list[pr.PersonRequest] | None, **kw: Any) -> HTMLResponse:
        code, result = _REFUSALS[reason]
        _audit(settings, user, decision, request_id, result=result, **kw)
        return _page(request, user, items, status_code=code, error=reason)

    if decision == "confirm" and not stepped_up(request):
        return refused("not_fresh", None)
    try:
        items = await _list(settings, token, cid)
    except pr.PersonRequestError as exc:
        return refused(exc.reason, None, code=exc.code)
    item = next((i for i in items if i.id == request_id), None)
    if item is None:
        return refused("not_found", items)
    if decision == "confirm" and item.own:
        # papaia-audit's own answer says this person filed it: no decision is sent.
        return refused("own_request", items, operation=item.operation)
    try:
        decided = await anyio.to_thread.run_sync(
            lambda: pr.decide(settings, request_id, decision, token, cid)
        )
    except pr.PersonRequestError as exc:
        return refused(exc.reason, items, operation=item.operation, code=exc.code)
    _audit(settings, user, decision, request_id, operation=item.operation)
    shown = [decided.request if i.id == request_id else i for i in items]
    return _page(request, user, shown, decided=decided)


@router.post(PAGE + "/{request_id}/confirm", response_class=HTMLResponse)
async def confirm_request(
    request: Request,
    request_id: str,
    user: ConfirmerUser,
    settings: SettingsDep,
    token: TokenDep,
) -> HTMLResponse:
    """Confirm one request after a fresh sign-in; an export answers its passphrase once."""
    return await _decide(request, request_id, "confirm", user, settings, token)


@router.post(PAGE + "/{request_id}/reject", response_class=HTMLResponse)
async def reject_request(
    request: Request,
    request_id: str,
    user: ConfirmerUser,
    settings: SettingsDep,
    token: TokenDep,
) -> HTMLResponse:
    """Reject one request; either of the two persons it names may."""
    return await _decide(request, request_id, "reject", user, settings, token)
