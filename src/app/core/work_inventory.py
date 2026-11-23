"""The inventory of work objects ("Bestand"): apps, workflows, metrics, dashboards and the
assistant's event rules and reminders, merged from the modules (governance GV-16, GV-17,
GV-21; OPS-24).

Not to be confused with `app.core.inventory`, which is the declared set of containers.

* Every module with work objects serves its part at `GET /internal/inventory` as a document
  of `papaia.inventory/1` (kit schema `inventory.v1.schema.json`, contract
  `contract/inventory.md`). The manager asks each of them on every request and keeps no copy.
* An answer is used only when it is a valid document of that schema, holds objects of the
  module that was asked and nothing else, each id once, and carries the checksum the contract
  defines. Anything else -- no answer, a broken or oversized answer, an unknown field -- makes
  the module `nicht erreichbar`: listed in `sources`, its objects missing, never a silent gap.
* The merged document is sorted by id (byte order) and carries the checksum over its
  canonical JSON, so two lists of an unchanged installation are equal.
* Filters narrow the items only; `sources` always names every module asked. A filtered
  document gets the checksum of its own content.

The inventory carries no content (no rows, no conditions, no reminder text); the schema
refuses a title, a version or sharing on an assistant entry, so a module that sends one is
treated as not answering.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import ssl
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from typing import Any

import httpx

# jsonschema comes with the kit (pinned in uv.lock), which validates its contracts with it too.
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from papaia_kit.correlation import CORRELATION_HEADER

from app.config import INVENTORY_MODULES
from app.core import kit_audit

logger = logging.getLogger(__name__)

FORMAT = "papaia.inventory/1"
REACHABLE = "erreichbar"
UNREACHABLE = "nicht erreichbar"
PATH = "/internal/inventory"
# The schema allows 100 000 objects in all; one module's part stays well below 64 MiB.
MAX_ANSWER_BYTES = 64 * 1024 * 1024
# Per network operation (connect, each read) ...
TIMEOUT_SECONDS = 10.0
# ... and for one module's whole answer: a module that keeps sending slowly is cut off here,
# so the page waits at most this (plus one read timeout) per module.
DEADLINE_SECONDS = 20.0

FLAGS = ("owner_inactive", "deputy_missing", "unused_180d", "confirmation_due", "class_above_role")
EFFECTS = ("read", "write", "send", "delete", "check")
# Least sensitive first, as the kit's data class scale.
DATA_CLASSES = (
    "public",
    "internal",
    "partner_contact",
    "personal",
    "employee",
    "confidential",
    "special",
    "professional_secret",
)
_SUB = re.compile(r"\A[\x21-\x7e]{1,255}\Z")

CSV_COLUMNS = (
    "id",
    "kind",
    "module",
    "title",
    "purpose",
    "version",
    "state",
    "owner_sub",
    "deputy_sub",
    "approver_sub",
    "approved_at",
    "shared_groups",
    "shared_persons",
    "catalog_entries",
    "max_data_class",
    "service_accounts",
    "created_at",
    "last_used_at",
    "confirmed_at",
    "flags",
)

Fetch = Callable[[str, str], bytes]


class SourceUnavailable(Exception):  # noqa: N818 - a state of a module, not a failure here
    """A module gave no usable answer. Carries the module name only, never the answer."""


@cache
def schema() -> dict[str, Any]:
    """The kit's `papaia.inventory/1` schema, from the vendored wheel."""
    text = (
        resources.files("papaia_kit")
        .joinpath("_data/schemas/inventory.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = json.loads(text)
    return loaded


@cache
def _validator() -> Draft202012Validator:
    Draft202012Validator.check_schema(schema())
    return Draft202012Validator(schema())


def checksum(body: Mapping[str, Any]) -> str:
    """`sha256:` over the canonical JSON of the document without `checksum` (contract)."""
    content = {k: v for k, v in body.items() if k != "checksum"}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _items_of(module: str, raw: bytes) -> list[dict[str, Any]]:
    """The objects of one module's answer, or SourceUnavailable when it is not usable."""
    if len(raw) > MAX_ANSWER_BYTES:
        raise SourceUnavailable(module)
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise SourceUnavailable(module) from None
    if not isinstance(doc, dict) or not _validator().is_valid(doc):
        raise SourceUnavailable(module)
    items: list[dict[str, Any]] = doc["items"]
    ids = [item["id"] for item in items]
    if (
        any(item["module"] != module for item in items)
        or len(set(ids)) != len(ids)
        or doc["checksum"] != checksum(doc)
    ):
        raise SourceUnavailable(module)
    return items


def _document(sources: list[dict[str, str]], items: list[dict[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": FORMAT,
        "generated_on": datetime.now(tz=UTC).strftime("%Y-%m-%d"),
        "sources": sources,
        "items": sorted(items, key=lambda item: str(item["id"]).encode()),
    }
    body["checksum"] = checksum(body)
    return body


def collect(
    source_map: Mapping[str, str], fetch: Fetch, *, also_ask: Iterable[str] = ()
) -> dict[str, Any]:
    """Ask every module in `source_map` (plus `also_ask`, the installed modules without an
    address, which are unreachable by definition) and merge the answers."""
    modules = sorted(set(source_map) | {m for m in also_ask if m in INVENTORY_MODULES})
    sources: list[dict[str, str]] = []
    items: list[dict[str, Any]] = []
    for module in modules:
        base = source_map.get(module)
        try:
            if base is None:
                raise SourceUnavailable(module)
            items.extend(_items_of(module, fetch(module, base + PATH)))
        except SourceUnavailable:
            logger.warning("inventory: %s gave no usable answer", module)
            sources.append({"module": module, "status": UNREACHABLE})
        else:
            sources.append({"module": module, "status": REACHABLE})
    return _document(sources, items)


@dataclass(frozen=True)
class Filters:
    """The page's filters (GV-21): flag, module, data class, effect and person."""

    flag: str | None = None
    module: str | None = None
    data_class: str | None = None
    effect: str | None = None
    person: str | None = None

    NAMES = ("flag", "module", "data_class", "effect", "person")

    @classmethod
    def parse(cls, pairs: Iterable[tuple[str, str]]) -> Filters:
        """From the query string; ValueError for an unknown, repeated or invalid value."""
        values: dict[str, str] = {}
        seen: set[str] = set()
        for name, value in pairs:
            if name not in cls.NAMES or name in seen:
                raise ValueError(name)
            seen.add(name)
            # An empty value is the form's "any": the name counts, the filter does not.
            if value == "":
                continue
            allowed = {
                "flag": FLAGS,
                "module": INVENTORY_MODULES,
                "data_class": DATA_CLASSES,
                "effect": EFFECTS,
            }.get(name)
            if (allowed is not None and value not in allowed) or (
                name == "person" and not _SUB.match(value)
            ):
                raise ValueError(name)
            values[name] = value
        return cls(**values)

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.NAMES if getattr(self, name)}

    def match(self, item: Mapping[str, Any]) -> bool:
        if self.flag and self.flag not in item["flags"]:
            return False
        if self.module and item["module"] != self.module:
            return False
        if self.data_class and item["max_data_class"] != self.data_class:
            return False
        if self.effect and self.effect not in {e["effect"] for e in item["catalog_entries"]}:
            return False
        people = {item.get("owner_sub"), item.get("deputy_sub"), item.get("approver_sub")}
        return not (self.person and self.person not in people)


def narrowed(doc: Mapping[str, Any], filters: Filters) -> dict[str, Any]:
    """The document with the matching items only; every source stays listed."""
    return _document(list(doc["sources"]), [i for i in doc["items"] if filters.match(i)])


def unreachable(doc: Mapping[str, Any]) -> list[str]:
    return [s["module"] for s in doc["sources"] if s["status"] == UNREACHABLE]


def effects(item: Mapping[str, Any]) -> list[str]:
    """The distinct effects of an object's bindings, widest first."""
    found = {entry["effect"] for entry in item["catalog_entries"]}
    return [e for e in ("delete", "send", "write", "check", "read") if e in found]


def csv_cells(item: Mapping[str, Any]) -> dict[str, str]:
    """One object as the cells of `CSV_COLUMNS` (not yet guarded against formulas)."""
    shared = item.get("shared_with") or {}
    return {
        "id": item["id"],
        "kind": item["kind"],
        "module": item["module"],
        "title": item.get("title", ""),
        "purpose": item.get("purpose", ""),
        "version": item.get("version", ""),
        "state": item["state"],
        "owner_sub": item["owner_sub"],
        "deputy_sub": item.get("deputy_sub", ""),
        "approver_sub": item.get("approver_sub", ""),
        "approved_at": item.get("approved_at", ""),
        "shared_groups": ";".join(shared.get("groups", [])),
        "shared_persons": str(shared["persons"]) if "persons" in shared else "",
        "catalog_entries": ";".join(
            f"{e['catalog_id']}:{'+'.join(e['operations'])}:{e['effect']}"
            for e in item["catalog_entries"]
        ),
        "max_data_class": item["max_data_class"],
        "service_accounts": ";".join(
            f"{a['name']}@{a['audience']}" for a in item["service_accounts"]
        ),
        "created_at": item["created_at"],
        "last_used_at": item.get("last_used_at", ""),
        "confirmed_at": item.get("confirmed_at", ""),
        "flags": ";".join(item["flags"]),
    }


def fetch_with(
    client: Callable[[], httpx.Client],
    *,
    deadline_seconds: float = DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Fetch:
    """`GET` with a client from `client`, size-limited and with a total deadline.

    Redirects are not followed. Each failure (TLS, connection, status other than 200,
    oversized or overdue body) is SourceUnavailable; neither the address nor the answer is
    logged. The correlation id of the request in effect goes along.
    """

    def fetch(module: str, url: str) -> bytes:
        headers = {"Accept": "application/json"}
        correlation_id = kit_audit.current_binding().correlation_id
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        give_up = clock() + deadline_seconds
        try:
            with (
                client() as http,
                http.stream("GET", url, headers=headers, follow_redirects=False) as response,
            ):
                if response.status_code != 200:
                    raise SourceUnavailable(module)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_ANSWER_BYTES or clock() > give_up:
                        raise SourceUnavailable(module)
                return bytes(body)
        except (OSError, ssl.SSLError, httpx.HTTPError):
            raise SourceUnavailable(module) from None

    return fetch


def mtls_fetch(*, ca_file: str, cert_file: str, key_file: str) -> Fetch:
    """`fetch_with` over mutual TLS with the manager's platform client certificate."""

    def client() -> httpx.Client:
        context = ssl.create_default_context(cafile=ca_file)
        context.load_cert_chain(cert_file, key_file)
        return httpx.Client(verify=context, timeout=TIMEOUT_SECONDS, follow_redirects=False)

    return fetch_with(client)
