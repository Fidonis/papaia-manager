"""A text must be translated where it is shown, never where the module is imported.

`_("...")` at module level, in a class body or in a default argument runs once, at import, in
the source language, and the result is then served to everybody. Such a text is marked with
`N_("...")` (which returns it unchanged) and translated where it is rendered.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "src" / "app"

TRANSLATING = frozenset({"_", "gettext", "ngettext", "pgettext", "npgettext"})


class _Eager(ast.NodeVisitor):
    """Finds translating calls that run when a module is imported."""

    def __init__(self) -> None:
        self.found: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in TRANSLATING:
            self.found.append((node.lineno, name))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._function(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        # What is evaluated at definition time: decorators, defaults and annotations.
        eager: list[ast.AST] = [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]
        if not isinstance(node, ast.Lambda):
            eager += [*node.decorator_list]
            if node.returns:
                eager.append(node.returns)
        for part in eager:
            self.visit(part)
        # The body runs when the function is called, which is the right moment.


def eager_calls(source: str) -> list[tuple[int, str]]:
    visitor = _Eager()
    visitor.visit(ast.parse(source))
    return visitor.found


def _modules() -> list[Path]:
    # The package that defines the functions does not call them on import.
    return sorted(p for p in APP.rglob("*.py") if "i18n" not in p.relative_to(APP).parts[:1])


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.relative_to(APP).as_posix())
def test_no_text_is_translated_at_import_time(path: Path) -> None:
    found = eager_calls(path.read_text(encoding="utf-8"))

    assert not found, (
        f"{path.relative_to(APP).as_posix()}: translated at import time (use N_() here and "
        f"translate where the text is shown): {found}"
    )


# The check itself: it has to see what it is meant to forbid, and leave alone what is fine.


@pytest.mark.parametrize(
    "source",
    [
        'LABEL = _("Hello")',
        'TABLE = {"a": _("Hello")}',
        'class C:\n    label = _("Hello")',
        'def f(title=_("Hello")):\n    return title',
        'def f(*, title=gettext("Hello")):\n    return title',
        '@register(_("Hello"))\ndef f():\n    ...',
        'ITEMS = [ngettext("one", "many", 2) for _ in range(2)]',
        'F = lambda x=_("Hello"): x',
        'from app import i18n\nX = i18n.gettext("Hello")',
    ],
)
def test_the_check_sees_a_text_translated_on_import(source: str) -> None:
    assert eager_calls(source)


@pytest.mark.parametrize(
    "source",
    [
        'def f():\n    return _("Hello")',
        'async def f():\n    raise ValueError(_("Hello"))',
        'class C:\n    def label(self):\n        return gettext("Hello")',
        'TABLE = {"a": N_("Hello")}',
        'F = lambda: _("Hello")',
        'def f(title="Hello"):\n    return _(title)',
        'for _ in range(3):\n    pass',
    ],
)
def test_the_check_leaves_a_text_translated_on_use_alone(source: str) -> None:
    assert not eager_calls(source)
