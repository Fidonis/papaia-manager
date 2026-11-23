"""The strict content security policy of the manager, in report-only mode (T-UI-03).

Every HTML response carries the target policy as `Content-Security-Policy-Report-Only`. The
browser reports what the pages still do against it -- inline scripts, inline event handlers,
Alpine expressions that need `eval`, inline styles -- without blocking anything; the pages
are converted group by group and the policy is enforced once they all are. No report
endpoint: the browser check (scripts/csp-check.sh) collects the reports, so the policy adds no
input surface. A response that brings a policy of its own (the logo route) keeps it.

Which pages run on the CSP build of Alpine is the `converted` list of tests/csp_baseline.json,
the file the ratchet of the tests keeps; the image carries a copy next to this module.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

from jinja2 import pass_context
from jinja2.runtime import Context
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
HEADER = b"content-security-policy-report-only"
_OWN_POLICY = frozenset({b"content-security-policy", HEADER})

_HERE = Path(__file__).resolve().parent
# The copy the image carries (docker/Dockerfile), then the file of a source checkout.
_BASELINE_PATHS = (_HERE / "csp_baseline.json", _HERE.parents[1] / "tests" / "csp_baseline.json")
_ISLAND_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# What a bound value may name: a property path of the component, or one of its methods called
# without arguments -- nothing the CSP build of Alpine would have to evaluate as code.
_BOUND_EXPRESSION = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:\(\))?")


class ReportOnlyPolicyMiddleware:
    """Add the report-only policy to every HTML response that has no policy of its own."""

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
                if content_type.lower().startswith(b"text/html") and not names & _OWN_POLICY:
                    headers.append((HEADER, POLICY.encode("ascii")))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_policy)


@cache
def converted_templates() -> frozenset[str]:
    """The templates that run on the CSP build of Alpine."""
    for path in _BASELINE_PATHS:
        if path.is_file():
            doc = json.loads(path.read_text(encoding="utf-8"))
            names = doc.get("converted")
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ValueError(f"{path}: `converted` must be a list of template names")
            return frozenset(names)
    raise FileNotFoundError("csp_baseline.json is missing next to app/csp.py and in tests/")


@pass_context
def strict_page(context: Context) -> bool:
    """Whether the page being rendered is converted (its top-level template is listed)."""
    return context.name in converted_templates()


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
