"""The content security policy of the manager, enforced (T-UI-03, oberfl 3.3, P4).

Every HTML response carries `Content-Security-Policy`: scripts and styles only from the
manager's own files, no `unsafe-inline` and no `unsafe-eval` in any directive, and
`frame-ancestors` only the manager's own host. The pages have no inline script, no inline event
handler, no `<style>` block and no `style` attribute (tests/test_csp_zero.py), and run the CSP
build of Alpine, which evaluates no expression as code. htmx evaluates nothing and runs no
script of a swapped fragment (the `htmx-config` meta element of base.html). No report endpoint:
the browser check (scripts/csp-check.sh) collects violations, so the policy adds no input
surface. A response that brings an enforced policy of its own (the logo route) keeps it.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape
from starlette.types import ASGIApp, Message, Receive, Scope, Send

POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'self'",
    )
)
HEADER = b"content-security-policy"

_ISLAND_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# What a bound value may name: a property path of the component, or one of its methods called
# without arguments -- nothing the CSP build of Alpine would have to evaluate as code.
_BOUND_EXPRESSION = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:\(\))?")


class PolicyMiddleware:
    """Enforce the policy on every HTML response that has no enforced policy of its own."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}
                content_type = next(
                    (value for name, value in headers if name.lower() == b"content-type"), b""
                )
                if content_type.lower().startswith(b"text/html") and HEADER not in names:
                    headers.append((HEADER, POLICY.encode("ascii")))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_policy)


def island_id(value: object) -> str:
    """The id of a data island: a plain name, so it never needs escaping in the markup."""
    if not isinstance(value, str) or not _ISLAND_ID.fullmatch(value):
        raise ValueError(f"island id must be a name of letters, digits, - and _: {value!r}")
    return value


def bound_sentence(sentence: object, **expressions: str) -> Markup:
    """A sentence of the catalogue whose `{name}` values Alpine fills in as text.

    Replaces `x-html="fmtHtml(sentence, values)"`, which the CSP build of Alpine refuses: the
    server renders the sentence with its markup, and each value becomes
    `<span x-text="expression">`, which the browser fills as a text node, so a value never
    turns into markup. `sentence` must be a translation of the catalogue (`_()` in a template
    with autoescape returns Markup); a plain string is refused rather than trusted. A
    placeholder without an expression stays as it is.
    """
    if not isinstance(sentence, Markup):
        raise TypeError("bound_sentence takes a sentence of the catalogue (Markup)")
    for name, expression in expressions.items():
        if not isinstance(expression, str) or not _BOUND_EXPRESSION.fullmatch(expression):
            raise ValueError(f"bound value {name!r} must be a property path: {expression!r}")

    def fill(match: re.Match[str]) -> str:
        expression = expressions.get(match.group(1))
        if expression is None:
            return match.group(0)
        return f'<span x-text="{escape(expression)}"></span>'

    return Markup(_PLACEHOLDER.sub(fill, str(sentence)))
