"""Finds the text in a Jinja2 template that a reader sees and that is not marked for translation.

Not a test by itself: `test_i18n_templates_coverage.py` uses it to fail on untranslated text, and
it is a plain module (no pytest) so a script can use it as well.

The scan is a lexer, not a parser. It splits a template into Jinja comments, statements and
expressions, HTML tags, and text, then groups the text and the inline tags around it into the
"runs" a translator would see as one sentence. What it reports is a heuristic, and the allowlist
below is where a word that is not English prose (a product name, a unit) is excused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Elements that stay inside a sentence. Everything else (div, p, li, td, button, label, ...) ends
# one, and so does every Jinja statement.
INLINE_TAGS = frozenset(
    {"a", "b", "strong", "em", "i", "code", "kbd", "span", "small", "u", "sup", "sub", "mark",
     "abbr", "s", "del", "ins", "samp", "var", "q", "cite", "time"}
)
# What is inside these is never prose.
CODE_TAGS = frozenset({"code", "kbd", "samp", "var", "pre", "textarea"})
RAW_TAGS = frozenset({"script", "style"})
# Attributes whose value a reader sees or hears.
TEXT_ATTRS = frozenset({"title", "aria-label", "placeholder", "alt", "label", "aria-description"})

# Words that are not English prose: products, protocols, units. Case-insensitive.
ALLOWED_WORDS = frozenset(
    w.lower()
    for w in (  # noqa: SIM905 -- one readable block of words
        "papAIa Fidonis Keycloak Qdrant LibreChat LiteLLM LocalAI SearXNG n8n Paperless Docker "
        "Compose Nginx Ollama Postfix Mailpit Grafana Prometheus Loki Traefik Caddy GitHub GHCR "
        "OIDC OAuth OAuth2 SSO LDAP SMTP IMAP TLS SSL HTTP HTTPS API REST JSON YAML YML CSV XML "
        "URL URI UUID ID IDs TCP UDP DNS IP CPU GPU RAM SSD NVMe LLM RAG MCP CSRF CORS JWT SHA "
        "MB GB KB TB KiB MiB GiB TiB ms px px rem GPT env ENV PDF PNG SVG JPEG WebP HTML UTF "
        "ok kbit Mbit sec min hr UTC GMT cron systemd Linux Windows macOS Vulkan CUDA ROCm "
        "SKU Basic Bearer Authorization Content-Type localhost"
    ).split()
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’]*")
_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);")
_IDENT_CHAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


@dataclass
class Tok:
    kind: str  # text jexpr jstmt jcomment hcomment tag script svg opaque
    text: str
    start: int
    end: int
    name: str = ""  # tag name (lowercase) or statement keyword
    closing: bool = False
    selfclosing: bool = False


@dataclass
class Run:
    """Text, expressions and inline tags between two boundaries: what a translator sees."""

    tokens: list[Tok] = field(default_factory=list)
    # Next to an element that is filled while the page runs, so it reads as one sentence with it.
    touches_inserted: bool = False

    @property
    def start(self) -> int:
        return self.tokens[0].start

    @property
    def end(self) -> int:
        return self.tokens[-1].end


def _skip_delimited(src: str, i: int, close: str) -> int:
    """Index just after `close`, starting inside a Jinja delimiter; quotes are respected."""
    n = len(src)
    quote = ""
    while i < n:
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif src.startswith(close, i):
            return i + len(close)
        i += 1
    return n


def _scan_tag_end(src: str, i: int) -> int:
    """Index just after the `>` that closes the tag opened at `i` (a `<`)."""
    n = len(src)
    quote = ""
    i += 1
    while i < n:
        if src.startswith("{{", i):
            i = _skip_delimited(src, i + 2, "}}")
            continue
        if src.startswith("{%", i):
            i = _skip_delimited(src, i + 2, "%}")
            continue
        if src.startswith("{#", i):
            j = src.find("#}", i + 2)
            i = n if j < 0 else j + 2
            continue
        c = src[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == ">":
            return i + 1
        i += 1
    return n


_STMT_NAME = re.compile(r"\{%-?\s*([A-Za-z_]+)")
_END_TRANS = re.compile(r"\{%-?\s*endtrans\b[^%]*%\}")
_END_RAW = re.compile(r"\{%-?\s*endraw\b[^%]*%\}")


def tokenize(src: str) -> list[Tok]:
    toks: list[Tok] = []
    n = len(src)
    i = 0
    text_start = 0

    def push_text(upto: int) -> None:
        if upto > text_start:
            toks.append(Tok("text", src[text_start:upto], text_start, upto))

    while i < n:
        if src.startswith("{#", i):
            push_text(i)
            j = src.find("#}", i + 2)
            j = n if j < 0 else j + 2
            toks.append(Tok("jcomment", src[i:j], i, j))
            i = text_start = j
        elif src.startswith("{{", i):
            push_text(i)
            j = _skip_delimited(src, i + 2, "}}")
            toks.append(Tok("jexpr", src[i:j], i, j))
            i = text_start = j
        elif src.startswith("{%", i):
            push_text(i)
            j = _skip_delimited(src, i + 2, "%}")
            match = _STMT_NAME.match(src, i)
            name = match.group(1) if match else ""
            if name in ("trans", "raw"):
                end_re = _END_TRANS if name == "trans" else _END_RAW
                found = end_re.search(src, j)
                j = found.end() if found else n
                toks.append(Tok("opaque", src[i:j], i, j, name=name))
            else:
                toks.append(Tok("jstmt", src[i:j], i, j, name=name))
            i = text_start = j
        elif src.startswith("<!--", i):
            push_text(i)
            j = src.find("-->", i + 4)
            j = n if j < 0 else j + 3
            toks.append(Tok("hcomment", src[i:j], i, j))
            i = text_start = j
        elif src.startswith("<!", i):
            # <!doctype html> and the like: markup, not text.
            push_text(i)
            j = src.find(">", i + 2)
            j = n if j < 0 else j + 1
            toks.append(Tok("hcomment", src[i:j], i, j))
            i = text_start = j
        elif src[i] == "<" and i + 1 < n and (src[i + 1].isalpha() or src[i + 1] == "/"):
            push_text(i)
            j = _scan_tag_end(src, i)
            raw = src[i:j]
            closing = raw.startswith("</")
            match = re.match(r"</?\s*([A-Za-z][A-Za-z0-9:-]*)", raw)
            name = match.group(1).lower() if match else ""
            selfclosing = raw.rstrip(">").rstrip().endswith("/") or name in {
                "br", "hr", "img", "input", "meta", "link", "source", "wbr"
            }
            if not closing and name in RAW_TAGS | {"svg"}:
                close = re.compile(rf"</{name}\s*>", re.IGNORECASE).search(src, j)
                k = close.end() if close else n
                toks.append(Tok("script" if name in RAW_TAGS else "svg", src[i:k], i, k, name=name))
                i = text_start = k
                continue
            toks.append(Tok("tag", raw, i, j, name=name, closing=closing, selfclosing=selfclosing))
            i = text_start = j
        else:
            i += 1
    push_text(n)
    return toks


def line_of(src: str, offset: int) -> int:
    return src.count("\n", 0, offset) + 1


_SENTENCE_ATTRS = frozenset({"class", "href", "target", "rel", "for", "lang"})
# Elements whose content is put there while the page runs: a sentence around one cannot be
# translated as a whole, so a run next to one is left to be handled by hand.
_INSERTED_ATTRS = frozenset({"x-text", "x-html"})


def _inline_kind(tok: Tok) -> str:
    """`sentence` for an inline tag that can stay inside a message, else `break`/`inserted`.

    A tag with behaviour or a computed class (Alpine, htmx, an id, a Jinja class expression) is
    not part of the wording, so it ends the sentence instead of being copied into the message.
    """
    if tok.closing:
        return "sentence"
    names = {name.lower() for name, _v, _s, _e in parse_attrs(tok.text)}
    # An attribute without a value (`x-cloak`, `hidden`) is still behaviour.
    bare = {
        part.lower()
        for part in re.findall(r"\s([A-Za-z_:@.][\w:@.-]*)(?=[\s/>])", tok.text)
        if part.lower() not in {tok.name}
    }
    names |= bare
    if names & _INSERTED_ATTRS:
        return "inserted"
    if not names <= _SENTENCE_ATTRS:
        return "break"
    for name, value, _s, _e in parse_attrs(tok.text):
        if name.lower() == "class" and ("{{" in value or "{%" in value):
            return "break"
    return "sentence"


def _is_empty_element(toks: list[Tok], index: int) -> bool:
    """Whether the tag at `index` is followed directly by its own closing tag."""
    opener = toks[index]
    for tok in toks[index + 1 :]:
        if tok.kind == "text" and not tok.text.strip():
            continue
        return tok.kind == "tag" and tok.closing and tok.name == opener.name
    return False


def runs(toks: list[Tok]) -> list[Run]:
    """Group tokens into sentences. A run may hold text, `{{ }}` and inline tags only."""
    out: list[Run] = []
    current = Run()
    # name -> stack of kinds of the open inline tags, to give a closing tag its opener's kind.
    open_kinds: dict[str, list[str]] = {}
    last_inserted = False  # the previous significant token was an inserted-content element

    def flush() -> None:
        nonlocal current
        if current.tokens:
            out.append(current)
        current = Run()

    for index, tok in enumerate(toks):
        if tok.kind == "text" and not tok.text.strip():
            if current.tokens:
                current.tokens.append(tok)
            continue
        kind = "other"
        if tok.kind == "tag" and tok.name in INLINE_TAGS:
            if tok.closing:
                stack = open_kinds.get(tok.name) or []
                kind = stack.pop() if stack else "sentence"
            else:
                kind = _inline_kind(tok)
                if kind == "break" and _is_empty_element(toks, index):
                    # `<span id="count"></span>`: nothing in it yet, it is filled in later.
                    kind = "inserted"
                if not tok.selfclosing:
                    open_kinds.setdefault(tok.name, []).append(kind)
        elif tok.kind in ("text", "jexpr"):
            kind = "sentence"
        if kind == "sentence":
            if not current.tokens and last_inserted:
                current.touches_inserted = True
            current.tokens.append(tok)
            last_inserted = False
        else:
            if kind == "inserted" and current.tokens:
                current.touches_inserted = True
            flush()
            last_inserted = kind == "inserted"
    flush()
    return out


def prose_words(text: str) -> list[str]:
    """The words of `text` that are English prose rather than names, units or codes."""
    text = _ENTITY_RE.sub(" ", text)
    words = []
    for word in _WORD_RE.findall(text):
        if len(word) < 2 or word.lower() in ALLOWED_WORDS:
            continue
        words.append(word)
    return words


_PATHLIKE = re.compile(r"^[\w./{}-]*/[\w./{}-]*$")


def run_prose(run: Run) -> str:
    """The text of a run that a reader reads as prose: not inside <code>, not an expression."""
    depth = 0
    parts: list[str] = []
    for tok in run.tokens:
        if tok.kind == "tag":
            if tok.name in CODE_TAGS:
                depth += -1 if tok.closing else 1
        elif tok.kind == "text" and depth <= 0:
            parts.append(tok.text)
    prose = " ".join(parts)
    # A path or a URL on its own is not a sentence.
    return "" if _PATHLIKE.match(prose.strip()) else prose


def translatable(run: Run) -> bool:
    return bool(prose_words(run_prose(run)))


def parse_attrs(tag: str) -> list[tuple[str, str, int, int]]:
    """`(name, value, value_start, value_end)` of each quoted attribute in a tag's source."""
    found = []
    for match in re.finditer(r"""([A-Za-z_:@.][\w:@.-]*)\s*=\s*("([^"]*)"|'([^']*)')""", tag):
        value = match.group(3) if match.group(3) is not None else match.group(4)
        group = 3 if match.group(3) is not None else 4
        found.append((match.group(1), value, match.start(group), match.end(group)))
    return found


def attr_prose(value: str) -> str:
    """The prose of an attribute value: Jinja removed."""
    value = re.sub(r"\{\{.*?\}\}", " ", value, flags=re.DOTALL)
    return re.sub(r"\{%.*?%\}", " ", value, flags=re.DOTALL)


@dataclass
class Finding:
    line: int
    kind: str  # text | attr | script | alpine | expr
    text: str


_ALPINE_ATTR = re.compile(r"^(?::|x-bind:|x-text$|x-html$|x-tooltip)")
_JS_STRING = re.compile(r"""(?<![\w$])(["'])((?:\\.|(?!\1).){3,}?)\1""")
# A literal that reads as a sentence or a label: has a space, or starts upper-case.
_HUMAN_LITERAL = re.compile(
    r"^[A-Z][A-Za-z][A-Za-z '’.,:;!?()/&-]*$|^[A-Za-z]+(?: [A-Za-z’']+)+[.!?:]?$"
)


# HTTP methods, header names and camelCase identifiers (DOMContentLoaded, readyState): not text.
_NOT_TEXT = re.compile(
    r"^(?:GET|POST|PUT|DELETE|PATCH|HEAD|[A-Z][A-Za-z]*(?:-[A-Za-z]+)+|[A-Za-z]*[a-z][A-Z][A-Za-z]*)$"
)


def human_literals(code: str) -> list[str]:
    """String literals in a piece of JavaScript or a Jinja expression that read as text."""
    found = []
    for match in _JS_STRING.finditer(code):
        value = match.group(2)
        if _NOT_TEXT.match(value):
            continue
        if _HUMAN_LITERAL.match(value) and prose_words(value):
            found.append(value)
    return found


_WORD_LITERAL = re.compile(r"""(["'])([A-Za-z][A-Za-z '’-]*)\1""")
# What a template prints for a flag the page's script reads: not text.
_PRINTED_VALUES = frozenset({"true", "false", "null", "none", "undefined", "floor", "ceil"})
_BRANCH_BEFORE = re.compile(r"(?:\belse|\bor|\bdefault\()\s*\(?\s*$")
_BRANCH_AFTER = re.compile(r"\s*\)?\s*if\b")


def printed_words(expression: str) -> list[str]:
    """Plain words an expression prints: the branches of an inline `if`, an `or` fallback, or a
    literal that is the whole value ({{ 'unknown' }}). Words only compared with are not shown."""
    found = []
    body = expression[2:-2].strip() if expression.startswith("{{") else expression
    for match in _WORD_LITERAL.finditer(body):
        word = match.group(2)
        if word.lower() in _PRINTED_VALUES or not prose_words(word):
            continue
        before, after = body[: match.start()], body[match.end() :]
        if (
            _BRANCH_BEFORE.search(before)
            or _BRANCH_AFTER.match(after)
            or body == match.group(0)
        ):
            found.append(word)
    return found


_JS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\"'\w])//[^\n]*", re.DOTALL)


def _blank_comments(code: str) -> str:
    """JavaScript comments replaced by spaces, so offsets stay and their quotes are not read."""
    return _JS_COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), code)


def scan(src: str) -> list[Finding]:
    """Everything a reader would see in English that is not marked for translation."""
    toks = tokenize(src)
    findings: list[Finding] = []

    for run in runs(toks):
        if translatable(run):
            text = re.sub(r"\s+", " ", run_prose(run)).strip()
            findings.append(Finding(line_of(src, run.start), "text", text))

    for tok in toks:
        if tok.kind == "tag" and not tok.closing:
            for name, value, _start, _end in parse_attrs(tok.text):
                lowered = name.lower()
                if lowered in TEXT_ATTRS or (
                    lowered == "value"
                    and tok.name == "input"
                    and re.search(r"type\s*=\s*[\"'](?:submit|button)", tok.text)
                ):
                    if "_(" in value or "trans" in value:
                        continue
                    if prose_words(attr_prose(value)):
                        findings.append(Finding(line_of(src, tok.start), "attr", f"{name}={value}"))
                elif _ALPINE_ATTR.match(lowered) or lowered.startswith(("@", "x-on")):
                    for literal in human_literals(value):
                        if "_(" in value:
                            continue
                        findings.append(Finding(line_of(src, tok.start), "alpine", literal))
        elif tok.kind == "script":
            body = _blank_comments(tok.text)
            for match in _JS_STRING.finditer(body):
                value = match.group(2)
                if _NOT_TEXT.match(value):
                    continue
                if _HUMAN_LITERAL.match(value) and prose_words(value):
                    # A literal that is the argument of a gettext call is already marked.
                    before = body[max(0, match.start() - 12) : match.start()]
                    if "_(" in before or "I18N" in before:
                        continue
                    line = line_of(src, tok.start + match.start())
                    findings.append(Finding(line, "script", value))
        elif tok.kind in ("jexpr", "jstmt"):
            inner = tok.text
            if "_(" in inner:
                continue
            for literal in human_literals(inner):
                findings.append(Finding(line_of(src, tok.start), "expr", literal))
            if tok.kind == "jexpr":
                for literal in printed_words(inner):
                    findings.append(Finding(line_of(src, tok.start), "expr", literal))
        elif tok.kind == "opaque" and tok.name == "trans":
            # The variables of a {% trans %} are values, not text: a word handed in here
            # ({% trans value=('day' if n == 1 else 'days') %}) is a sentence built in pieces.
            head = tok.text[: tok.text.find("%}") + 2]
            for match in _WORD_LITERAL.finditer(head):
                word = match.group(2)
                before = head[: match.start()].rstrip()
                if word.lower() in _PRINTED_VALUES or before.endswith(("_(", "[")):
                    continue  # an option of a filter, a key, or a text that is marked
                if prose_words(word):
                    findings.append(Finding(line_of(src, tok.start), "expr", word))
    return findings
