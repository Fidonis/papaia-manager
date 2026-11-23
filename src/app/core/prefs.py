"""Language and appearance from the platform's preference service `prefs` (T-UI-02, PLT-28).

While the platform catalog lists Mein Bereich (core profile `me`, which runs `prefs`), `prefs` is
the one source of the interface language and the appearance of every module (interface spec
3.3, architecture D-02). The manager then

- reads the values at sign-in (`GET /api/v1/preferences/me`) and keeps them in the session
  (`pk_prefs`, kit `papaia_kit.ui.prefs`);
- compares, before every page, the etag of the shared session cookie `pk_ui` with the session
  and reads `prefs` again when another module changed a value (`PreferencesMiddleware`), so a
  change made in Studio or Mein Bereich shows on the manager's next page without a new sign-in;
- writes the choices of the frame's footer and account menu with `PUT
  /api/v1/preferences/me` (`If-Match`), sets `pk_ui` and records one `papaia.audit/1` row per
  attempt, with the field name and never the value (`write`).

Every call carries the signed-in person's own token, exchanged for the audience
`papaia-prefs` with the manager's client (RFC 8693, architecture W-17); `prefs` decides on it,
the manager holds no right of its own there. The token claims (`locale`, `zoneinfo`) and the
manager's own language file are not read in this mode.

Without Mein Bereich there is no `prefs` to ask: the manager keeps the appearance in the signed
session and the language in `manager/preferences.yaml`, as before.

`prefs` that cannot be reached never stops a page: it renders with the values the session last
held, else with the platform defaults (appearance of the system, language of the browser), and
`prefs` is not asked again for `RETRY_AFTER` seconds for that person.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import anyio
from papaia_kit.correlation import new_correlation_id
from papaia_kit.errors import ErrorClass
from papaia_kit.exchange import ClientSecret, ExchangeClient, ExchangeError, Transport
from papaia_kit.ui.prefs import (
    AUDIENCE,
    SESSION_KEY,
    JsonTransport,
    Preferences,
    PreferencesClient,
    PreferencesError,
    appearance_change,
    current_preferences,
    language_change,
    load_on_login,
    parse_preferences,
    write_preferences,
)
from starlette.requests import Request

from app.auth.user_token import UserTokenUnavailable, user_access_token
from app.core import kit_audit
from app.core.module_catalog import platform_catalog
from app.i18n.negotiate import PLATFORM_LANGUAGE_KEY as LANGUAGE_KEY

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from app.config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "LANGUAGE_KEY",
    "RETRY_AFTER",
    "SCOPE_KEY",
    "PreferencesMiddleware",
    "View",
    "WriteFailed",
    "Wiring",
    "active",
    "load_on_sign_in",
    "reset",
    "use",
    "view_of",
    "write",
]

# Where a request carries what `prefs` said, for the frame (`app.templating.pk_shell`); the
# language setting goes under LANGUAGE_KEY for `app.i18n.middleware`.
SCOPE_KEY = "papaia.prefs"
# How long `prefs` is left alone for one person after it could not be read.
RETRY_AFTER = 30.0
TIMEOUT = 5.0
# No page is rendered under these paths, or the sign-in reads the values itself.
_NO_READ = ("/static/", "/_kit/", "/health", "/auth/")
_MAX_HELD = 1024

Kind = Literal["appearance", "language"]


@dataclass(frozen=True, slots=True)
class View:
    """What a request renders with while `prefs` is the source; None: no values known."""

    preferences: Preferences | None

    @property
    def appearance(self) -> str:
        return self.preferences.appearance if self.preferences is not None else "system"

    @property
    def language(self) -> str:
        return self.preferences.locale if self.preferences is not None else "browser"


@dataclass(frozen=True, slots=True)
class Wiring:
    """The two clients a call to `prefs` needs: `prefs` itself and the token exchange."""

    client: PreferencesClient
    exchange: ExchangeClient

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        issuer_transport: Transport | None = None,
        prefs_transport: JsonTransport | None = None,
    ) -> Wiring:
        return cls(
            client=PreferencesClient(
                settings.prefs_url, transport=prefs_transport, timeout=TIMEOUT
            ),
            exchange=ExchangeClient(
                token_endpoint=settings.oidc_issuer_kc_token,
                client_id=settings.manager_oidc_client_id,
                auth=ClientSecret(settings.manager_oidc_client_secret),
                transport=issuer_transport,
                timeout=TIMEOUT,
            ),
        )


_wiring: Wiring | None = None
_wiring_lock = threading.Lock()
# person (`sub`) -> monotonic time before which `prefs` is not asked again
_held: dict[str, float] = {}


def use(wiring: Wiring | None) -> None:
    """Use these clients from now on; None builds them from the settings on the next call."""
    global _wiring  # noqa: PLW0603
    with _wiring_lock:
        _wiring = wiring


def reset() -> None:
    """Forget the clients and every hold-back (tests, and a changed configuration)."""
    use(None)
    _held.clear()


def _clients(settings: Settings) -> Wiring:
    global _wiring  # noqa: PLW0603
    with _wiring_lock:
        if _wiring is None:
            _wiring = Wiring.from_settings(settings)
        return _wiring


def active(settings: Settings) -> bool:
    """Whether `prefs` is the source: the platform catalog lists Mein Bereich (profile `me`)."""
    catalog = platform_catalog(settings.papaia_config_dir)
    return catalog is not None and catalog.surface("me") is not None


def view_of(scope: Mapping[str, Any]) -> View | None:
    """The values of this request, or None when the manager keeps its own choices."""
    view = scope.get(SCOPE_KEY)
    return view if isinstance(view, View) else None


def _correlation_id() -> str:
    return kit_audit.current_binding().correlation_id or new_correlation_id()


def _stored(session: Mapping[str, Any]) -> Preferences | None:
    try:
        return parse_preferences(session.get(SESSION_KEY))
    except PreferencesError:
        return None


def _held_back(sub: str) -> bool:
    until = _held.get(sub)
    if until is None:
        return False
    if until > time.monotonic():
        return True
    _held.pop(sub, None)
    return False


def _hold_back(sub: str) -> None:
    now = time.monotonic()
    if len(_held) >= _MAX_HELD:
        for key in [k for k, until in _held.items() if until <= now] or list(_held)[:1]:
            _held.pop(key, None)
    _held[sub] = now + RETRY_AFTER


def _token_source(wiring: Wiring, access_token: str | None, cid: str) -> Callable[[], str]:
    """The person's token for `prefs`; a failure is a PreferencesError, so the kit records it."""

    def token() -> str:
        if access_token is None:
            raise PreferencesError(ErrorClass.UNAUTHENTICATED, "session", correlation_id=cid)
        try:
            return wiring.exchange.exchange(access_token, AUDIENCE, correlation_id=cid).access_token
        except ExchangeError as exc:
            raise PreferencesError(exc.error_class, "exchange", correlation_id=cid) from None

    return token


async def _access_token(request: Request) -> str | None:
    try:
        return await user_access_token(request)
    except UserTokenUnavailable:
        return None


class _ReadNeeded(Exception):  # noqa: N818 - a signal, not a failure
    """The session's values are missing or stale: `prefs` has to be read."""


def _no_token() -> str:
    raise _ReadNeeded


async def _current(request: Request, settings: Settings, sub: str) -> Preferences | None:
    session = request.session
    cookie = request.headers.get("cookie")
    wiring = _clients(settings)
    cid = _correlation_id()
    try:
        # Without a reason to read, the kit answers from the session and asks for no token.
        return current_preferences(
            session, cookie, client=wiring.client, token=_no_token, correlation_id=cid
        )
    except _ReadNeeded:
        pass
    if _held_back(sub):
        return _stored(session)
    access_token = await _access_token(request)
    token = _token_source(wiring, access_token, cid)

    def read() -> Preferences:
        return current_preferences(
            session, cookie, client=wiring.client, token=token, correlation_id=cid
        )

    try:
        return await anyio.to_thread.run_sync(read)
    except PreferencesError as exc:
        logger.warning(
            "preferences not read: %s (%s), correlation id %s", exc.error_class, exc.reason, cid
        )
        _hold_back(sub)
        return _stored(session)


class PreferencesMiddleware:
    """Puts the values of `prefs` into every request of a signed-in person.

    Pure ASGI inside the session middleware (it reads and updates the session) and outside the
    language middleware (which takes the language from here). It reads `prefs` only for a page
    (`GET`/`HEAD` outside static files, kit files and the sign-in); every other request uses
    the values the session holds.
    """

    def __init__(self, app: ASGIApp, *, settings: Callable[[Scope], Settings | None]) -> None:
        self.app = app
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not str(scope.get("path", "")).startswith("/static/"):
            await self._prepare(scope)
        await self.app(scope, receive, send)

    async def _prepare(self, scope: Scope) -> None:
        session = scope.get("session")
        user = session.get("user") if isinstance(session, dict) else None
        sub = user.get("sub") if isinstance(user, dict) else None
        settings = self._settings(scope)
        if not isinstance(sub, str) or not sub or settings is None or not active(settings):
            return
        assert isinstance(session, dict)
        path = str(scope.get("path", ""))
        if scope.get("method") in ("GET", "HEAD") and not path.startswith(_NO_READ):
            found = await _current(Request(scope), settings, sub)
        else:
            found = _stored(session)
        view = View(found)
        scope[SCOPE_KEY] = view
        scope[LANGUAGE_KEY] = view.language


async def load_on_sign_in(
    request: Request, settings: Settings, access_token: str | None
) -> None:
    """Read the person's values into the new session; a failure leaves the defaults."""
    request.session.pop(SESSION_KEY, None)
    if not active(settings):
        return
    wiring = _clients(settings)
    cid = _correlation_id()
    token = _token_source(wiring, access_token, cid)
    session: MutableMapping[str, Any] = request.session

    def read() -> Preferences:
        return load_on_login(session, wiring.client, token, correlation_id=cid)

    try:
        await anyio.to_thread.run_sync(read)
    except PreferencesError as exc:
        logger.warning(
            "preferences not read at sign-in: %s (%s), correlation id %s",
            exc.error_class,
            exc.reason,
            cid,
        )


class WriteFailed(Exception):  # noqa: N818 - the outcome the route turns into a status
    """`prefs` did not take the change; `status` is the HTTP status for the browser."""

    def __init__(self, status: int) -> None:
        super().__init__(f"preferences not written ({status})")
        self.status = status


# A refusal of the person's own request keeps its status; everything else is "not available".
_STATUS = {
    ErrorClass.INVALID_INPUT: 400,
    ErrorClass.UNAUTHENTICATED: 401,
    ErrorClass.IDENTITY_REVOKED: 401,
    ErrorClass.CONFLICT: 409,
}


async def write(
    request: Request,
    settings: Settings,
    *,
    sub: str,
    kind: Kind,
    value: str,
    audit: Callable[[Mapping[str, Any]], None],
) -> str:
    """Store one choice of the frame in `prefs`; returns the `Set-Cookie` value of `pk_ui`.

    `audit` receives the one `papaia.audit/1` row of the attempt (built and checked by the
    kit), whether it was allowed, refused or failed. Raises `WriteFailed` after a refusal.
    """
    change = appearance_change(value) if kind == "appearance" else language_change(value)
    wiring = _clients(settings)
    cid = _correlation_id()
    token = _token_source(wiring, await _access_token(request), cid)
    session: MutableMapping[str, Any] = request.session

    def run() -> str:
        return write_preferences(
            session,
            wiring.client,
            token,
            change,
            sub=sub,
            azp=settings.manager_oidc_client_id,
            writer=kit_audit.SOURCE,
            audit=audit,
            correlation_id=cid,
            domain=settings.prefs_cookie_domain,
        ).set_cookie

    try:
        cookie = await anyio.to_thread.run_sync(run)
    except PreferencesError as exc:
        logger.warning(
            "preferences not written: %s (%s), correlation id %s", exc.error_class, exc.reason, cid
        )
        raise WriteFailed(_STATUS.get(exc.error_class, 503)) from None
    _held.pop(sub, None)
    return cookie
