"""REST API -- the optional modules and the customer overlay (Settings > Modules, Overlay).

Administrator-only and CSRF-checked like every other mutating route. A change is a queued
job, so it is single-flight with everything else the manager does to the stack and its log can
be followed; checking and previewing a draft are answered at once and change nothing.

Overlay content can hold an API key. Bodies are read by hand, not through a Pydantic model whose
validation error would echo the input, and nothing here logs the content or puts it in an audit
entry (the audit entries of a change say target, size and revisions).

Stale pages are 409s: a module switch carries the `revision` of the profile set it was made
from, an overlay change the revision (SHA-256) of the file it was made from. A restore, an
upgrade or a stack action that is running refuses a change, because it replaces or migrates the
directory the change is written to; an ordinary job does not, the queue runs one at a time.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.config import history, modules, overlay, preview, service
from app.core.config.overlay import OverlayError
from app.core.ctl import LOCALAI_VARIANTS
from app.core.jobs import JobQueue
from app.core.mail.settings import (
    MailError,
    MailInput,
    MailSettings,
    load_mail_settings,
    validate_input,
)
from app.routers.stack_guard import require_idle_stack

router = APIRouter(prefix="/api/v1/config")


class MailValues(BaseModel):
    """The mail server, as the dialog asks for it when none is stored. The password is
    write-only, like on the Mail page."""

    host: str
    port: int | None = None
    security: str = "starttls"
    user: str | None = None
    from_address: str
    from_name: str | None = None
    password: str | None = None


class ModuleBody(BaseModel):
    revision: str
    variant: str | None = None
    # The answers to what `setup` asks when the module is switched on and its settings are
    # missing: the public URL (LocalAI, RAG), the mail server (mail).
    public_url: str | None = None
    mail: MailValues | None = None


class DisableBody(BaseModel):
    revision: str
    # What the switch-off deletes besides the containers: `keep`, `data` or `all`.
    scope: str = "keep"
    # The module's name, typed back to confirm a deletion.
    confirm: str | None = None


class DraftBody(BaseModel):
    content: str


class ApplyBody(BaseModel):
    content: str
    revision: str


class RollbackBody(BaseModel):
    version: str
    revision: str


def _queue() -> JobQueue:
    from app.main import _job_queue  # noqa: PLC0415

    if _job_queue is None:
        raise HTTPException(status_code=503, detail="job queue not initialized")
    return _job_queue


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


async def _body[T: BaseModel](request: Request, model: type[T]) -> T:
    """The request body as `model`, with a refusal that never repeats a value."""
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="The body is not valid JSON.") from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise HTTPException(status_code=422, detail=problems or "The body is not valid.") from exc


def _overlay_error(exc: OverlayError, *, code: int = 422) -> HTTPException:
    return HTTPException(status_code=code, detail=str(exc))


def _target(key: str) -> overlay.OverlayTarget:
    try:
        return overlay.target(key)
    except OverlayError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _stale(current_revision: str, given: str) -> None:
    if given != current_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The overlay file changed after this page loaded it. "
                "Reload the file to see the current version."
            ),
        )


def _read(settings: Settings, key: str) -> overlay.OverlayFile:
    try:
        file = overlay.read(settings.papaia_config_dir, key)
    except OverlayError as exc:
        raise _overlay_error(exc, code=409) from exc
    if file.raw is not None:
        try:
            file.raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=409, detail="The overlay file is not valid UTF-8. Edit it on the host."
            ) from exc
    return file


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------


@router.get("/modules")
async def get_modules(
    user: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    return {
        "revision": modules.revision(settings.papaia_config_dir),
        "modules": modules.state(settings.papaia_config_dir, settings.papaia_workspace_dir),
        "variants": [{"id": v, "label": modules.VARIANT_LABELS[v]} for v in LOCALAI_VARIANTS],
        "host_only_variants": list(modules.HOST_ONLY_VARIANTS),
    }


def _module_current(settings: Settings, name: str, revision: str) -> bool:
    """Whether `name` is switched on, after checking the page is not stale."""
    if name not in modules.SWITCHABLE:
        raise HTTPException(status_code=404, detail="This module can't be switched from here.")
    if revision != modules.revision(settings.papaia_config_dir):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The modules changed since this page was loaded. Reload and try again.",
        )
    return modules.SWITCHABLE[name].profile in modules.active(settings.papaia_config_dir)


@router.get("/modules/{name}/info")
async def get_module_info(
    name: str, user: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """What switching the module off could delete: its volumes (and whether they exist) and
    its settings, as the core reports them. `available` is false for a core that can't say."""
    if name not in modules.SWITCHABLE:
        raise HTTPException(status_code=404, detail="This module can't be switched from here.")
    return await modules.module_info(
        name,
        workspace_dir=settings.papaia_workspace_dir,
        config_dir=settings.papaia_config_dir,
    )


def _mail_input(
    name: str, values: MailValues | None, current: MailSettings
) -> MailInput | None:
    """The mail server of an enable request, validated like a save on the Mail page."""
    if values is None:
        return None
    if name != "mail":
        raise modules.ModuleError("Only the mail relay takes a mail server.")
    return validate_input(
        host=values.host,
        port=values.port,
        security=values.security,
        user=values.user,
        from_address=values.from_address,
        from_name=values.from_name,
        password=values.password,
        current=current,
    )


@router.post("/modules/{name}/enable", status_code=status.HTTP_202_ACCEPTED)
async def enable_module(
    name: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Switch a module on and start it. Answers with the job to follow."""
    verify_csrf(request)
    body = await _body(request, ModuleBody)
    if _module_current(settings, name, body.revision):
        raise HTTPException(status_code=409, detail="This module is already switched on.")
    current_mail = load_mail_settings(settings.papaia_config_dir)
    try:
        variant = modules.validate_enable(name, body.variant)
        public_url = modules.validate_public_url(name, body.public_url)
        mail = _mail_input(name, body.mail, current_mail)
        modules.check_enable(
            name,
            settings.papaia_config_dir,
            settings.papaia_workspace_dir,
            mail_given=mail is not None,
        )
    except (modules.ModuleError, MailError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await require_idle_stack()
    username = _user_id(user)
    # The URL is a setting; the mail password never is. Neither value of a job is a secret.
    job = await _queue().enqueue(
        action=service.ENABLE_ACTION,
        target=f"module:{name}",
        user=username,
        params={"module": name, "variant": variant, "public_url": public_url},
        callback=service.enable_callback(
            module=name,
            variant=variant,
            user=username,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            public_url=public_url,
            mail=mail,
            current_mail=current_mail,
        ),
    )
    return {"job_id": job.id, "status": "queued"}


@router.post("/modules/{name}/disable", status_code=status.HTTP_202_ACCEPTED)
async def disable_module(
    name: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Stop a module, remove its containers and switch it off. The body says whether its data
    and its settings are deleted too (`scope`: `keep`, `data`, `all`); a deletion needs the
    module's name in `confirm`."""
    verify_csrf(request)
    body = await _body(request, DisableBody)
    if not _module_current(settings, name, body.revision):
        raise HTTPException(status_code=409, detail="This module is not switched on.")
    try:
        scope = modules.validate_scope(
            name, body.scope, body.confirm, settings.papaia_workspace_dir
        )
    except modules.ModuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await require_idle_stack()
    username = _user_id(user)
    job = await _queue().enqueue(
        action=service.DISABLE_ACTION,
        target=f"module:{name}",
        user=username,
        params={"module": name, "scope": scope},
        callback=service.disable_callback(
            module=name,
            scope=scope,
            user=username,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        ),
    )
    return {"job_id": job.id, "status": "queued"}


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------


def target_json(settings: Settings, target: overlay.OverlayTarget) -> dict[str, Any]:
    try:
        file = overlay.read(settings.papaia_config_dir, target.key)
        problem = ""
    except OverlayError as exc:
        file = overlay.OverlayFile(target.key, False, None, "", None)
        problem = str(exc)
    services, _ = modules.service_count(settings.papaia_workspace_dir, target.profile)
    return {
        "key": target.key,
        "label": target.label,
        "path": target.path,
        "description": target.description,
        "profile": target.profile,
        "group_active": target.profile in modules.active(settings.papaia_config_dir),
        "services": services,
        "exists": file.exists,
        "revision": file.revision,
        "size": file.size,
        "lines": file.lines,
        "modified": file.modified.isoformat() if file.modified else None,
        "problem": problem,
    }


@router.get("/overlay")
async def list_overlay(
    user: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    return {
        "preview_available": preview.available(settings.papaia_workspace_dir),
        "targets": [target_json(settings, t) for t in overlay.TARGETS],
        "other": overlay.other_files(settings.papaia_config_dir),
    }


@router.get("/overlay/{key}")
async def get_overlay(
    key: str, user: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    target = _target(key)
    file = _read(settings, key)
    return {
        **target_json(settings, target),
        "content": file.text,
        "preview_available": preview.available(settings.papaia_workspace_dir),
        "versions": [v.as_json() for v in history.versions(settings.papaia_config_dir, key)],
    }


@router.post("/overlay/{key}/check")
async def check_overlay(
    key: str,
    request: Request,
    user: AdminUser,
) -> dict[str, Any]:
    """Syntax check of a draft, as you type. Nothing is written and nothing leaves the manager."""
    verify_csrf(request)
    _target(key)
    body = await _body(request, DraftBody)
    problems = overlay.check(body.content)
    return {
        "ok": not problems,
        "problems": [p.as_json() for p in problems],
        "summary": "" if problems else overlay.summary(body.content),
    }


@router.post("/overlay/{key}/preview")
async def preview_overlay(
    key: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """What the target renders to with the draft, against what it renders to now."""
    verify_csrf(request)
    target = _target(key)
    body = await _body(request, DraftBody)
    problems = overlay.check(body.content)
    if problems:
        return {"ok": False, "available": True, "problems": [p.as_json() for p in problems]}
    if not preview.available(settings.papaia_workspace_dir):
        return {
            "ok": False,
            "available": False,
            "error": (
                "This papAIa core can't preview the effective result yet. "
                "The syntax check above still applies."
            ),
        }
    try:
        result = await preview.run(
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            target_path=target.path,
            draft=body.content,
        )
    except preview.PreviewError as exc:
        return {"ok": False, "available": True, "error": str(exc)}
    if not result.ok:
        return {
            "ok": False,
            "available": True,
            "problems": [{"message": result.error, "line": result.line, "column": result.column}],
        }
    return {
        "ok": True,
        "available": True,
        "changed": result.changed,
        "added": result.added,
        "removed": result.removed,
        "diff": result.diff,
    }


async def _validated(settings: Settings, target: overlay.OverlayTarget, content: str) -> bytes:
    """The bytes to write, after the checks that can refuse them: the syntax check, and the
    core's own merge when it can preview. A core that can't be asked is not a reason to refuse
    (the job puts the previous file back if the start fails)."""
    problems = overlay.check(content)
    if problems:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "The overlay can't be saved: " + problems[0].message,
                "problems": [p.as_json() for p in problems],
            },
        )
    if preview.available(settings.papaia_workspace_dir):
        try:
            result = await preview.run(
                workspace_dir=settings.papaia_workspace_dir,
                config_dir=settings.papaia_config_dir,
                target_path=target.path,
                draft=content,
            )
        except preview.PreviewError:
            return content.encode("utf-8")
        if not result.ok:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "The core can't merge this overlay: " + result.error,
                    "problems": [
                        {"message": result.error, "line": result.line, "column": result.column}
                    ],
                },
            )
    return content.encode("utf-8")


async def _enqueue_overlay(
    settings: Settings,
    user: OIDCClaims,
    *,
    key: str,
    content: bytes | None,
    revision: str,
    event: str,
    version: str | None = None,
) -> dict[str, str]:
    actions = {
        "apply": service.APPLY_ACTION,
        "discard": service.DISCARD_ACTION,
        "rollback": service.ROLLBACK_ACTION,
    }
    username = _user_id(user)
    job = await _queue().enqueue(
        action=actions[event],
        target=f"overlay:{key}",
        user=username,
        params={"target": key},
        callback=service.overlay_callback(
            key=key,
            content=content,
            expected_revision=revision,
            event=event,
            user=username,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            version=version,
        ),
    )
    return {"job_id": job.id, "status": "queued"}


@router.put("/overlay/{key}", status_code=status.HTTP_202_ACCEPTED)
async def apply_overlay(
    key: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Save the draft and recreate the containers that read it. Answers with the job."""
    verify_csrf(request)
    target = _target(key)
    body = await _body(request, ApplyBody)
    _stale(_read(settings, key).revision, body.revision)
    data = await _validated(settings, target, body.content)
    await require_idle_stack()
    return await _enqueue_overlay(
        settings, user, key=key, content=data, revision=body.revision, event="apply"
    )


@router.delete("/overlay/{key}", status_code=status.HTTP_202_ACCEPTED)
async def discard_overlay(
    key: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    revision: str = "",
) -> dict[str, str]:
    """Remove the overlay file (after saving it to Versions) and recreate what reads it."""
    verify_csrf(request)
    _target(key)
    file = _read(settings, key)
    _stale(file.revision, revision)
    if not file.exists:
        raise HTTPException(status_code=409, detail="There is no overlay file to discard.")
    await require_idle_stack()
    return await _enqueue_overlay(
        settings, user, key=key, content=None, revision=revision, event="discard"
    )


@router.get("/overlay/{key}/versions")
async def list_versions(
    key: str, user: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    _target(key)
    return {"versions": [v.as_json() for v in history.versions(settings.papaia_config_dir, key)]}


@router.get("/overlay/{key}/versions/{version_id}")
async def get_version(
    key: str,
    version_id: str,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """One saved version: its content and how it differs from the file now."""
    _target(key)
    try:
        data = history.read(settings.papaia_config_dir, key, version_id)
    except OverlayError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    current = _read(settings, key)
    text = data.decode("utf-8", errors="replace")
    return {
        "id": version_id,
        "content": text,
        "diff": preview.diff_lines(current.text, text),
        "revision": current.revision,
    }


@router.post("/overlay/{key}/rollback", status_code=status.HTTP_202_ACCEPTED)
async def rollback_overlay(
    key: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Make a saved version the overlay again (the current file is saved first)."""
    verify_csrf(request)
    target = _target(key)
    body = await _body(request, RollbackBody)
    _stale(_read(settings, key).revision, body.revision)
    try:
        data = history.read(settings.papaia_config_dir, key, body.version)
    except OverlayError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="That version is not valid UTF-8.") from exc
    await _validated(settings, target, text)
    await require_idle_stack()
    return await _enqueue_overlay(
        settings,
        user,
        key=key,
        content=data,
        revision=body.revision,
        event="rollback",
        version=body.version,
    )
