"""Governance facts of the knowledge map: who owns an entry, how it is classified, whether it
holds personal data, and which tier each model has.

The operator keeps them in `overlay/knowledge-map/governance.yaml` (the `governance` operator
file), deliberately small:

* `sources.<id>`: `owner`, `classification`, `personal_data`, optional `data_subjects` and
  `ropa_ref` (a reference into the operator's own record of processing);
* `models.<alias>`: `tier` and optional `role`; a model that is not listed counts as
  `third_country` (fail-closed);
* `policy`: `allowed_tiers`, `personal_data_tiers`, `broad_roles`, `embedding_model`;
* `exceptions`: accepted findings, each with an expiry.

The core's render validates the file and evaluates every rule: its findings go into
`lints.yaml` (`category: governance`, each with a `fix` and structured `params`), next to the
add-on proposals, the effective `policy`, the codes an exception may name (`exceptable`) and
its evaluation of every agent (`agents`). The manager displays that and adds only what it alone
can see, the models LiteLLM actually serves (`origin: manager`).
Neither the map nor the manager restricts data access or model use: the target systems,
LiteLLM's teams and keys and the chat's agent bindings do. These are wiring checks.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import io
import ipaddress
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

from app.config import Settings
from app.core import knowledge_map as km
from app.core.knowledge_map import _ID_RE
from app.core.litellm import NO_KEY, LitellmClient, LitellmError, Problem, litellm_secrets
from app.core.model_usage import ModelEntry, parse_deployments
from app.core.qdrant import tls_verify
from app.i18n import N_
from app.i18n import gettext as _

TIERS = ("local", "eu_hosted", "third_country_contracted", "third_country")
CLASSES = ("public", "internal", "confidential", "strictly_confidential")
PERSONAL_DATA = ("none", "ordinary", "special", "criminal", "unknown")
SUBJECTS = ("employees", "applicants", "customers", "suppliers", "patients", "minors", "other")
SEVERITIES = ("high", "medium", "low")
# Fail-closed: a model nobody classified counts as the least trusted tier.
UNLISTED_TIER = "third_country"
DATA_WITH_RECORD = ("ordinary", "special", "criminal")

SOURCE_FIELDS = ("owner", "classification", "personal_data", "data_subjects", "ropa_ref")
PROPOSAL_FIELDS = ("classification", "personal_data", "data_subjects")
MODEL_FIELDS = ("tier", "role")
EXCEPTION_FIELDS = ("finding", "subject", "expires", "reason", "accepted_by_role", "risk_ref")
# The findings the manager makes itself; an exception may name them as well.
MANAGER_CODES = ("unclassified_model", "model_tier_contradiction")

# Suggestions of these kinds carry text taken from what people asked; accepting one needs the
# administrator to say it names nobody.
TEXT_SUGGESTIONS = frozenset({"example_query", "gold_question", "not_covered"})

EXPORTS = ("facts.json", "facts.csv")

# Providers whose LiteLLM default address is on the same host (no api_base needed).
_LOCAL_PROVIDERS = frozenset({"ollama", "ollama_chat"})
_INTERNAL_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa", ".localdomain")


class GovernanceError(Exception):
    """A governance change the store refuses. The message is shown as it is."""


# ── reading values ──────────────────────────────────────────────────────────────────────────


def as_date(value: Any) -> date | None:
    """A date from YAML (already a date) or an ISO string; None for anything else."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return date.fromisoformat(value.strip())
    return None


def jsonable(value: Any) -> Any:
    """The document with its dates as ISO strings, for a JSON answer."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


# ── the shape check ─────────────────────────────────────────────────────────────────────────


def check_document(
    document: dict[str, Any], exceptable: Iterable[str] | None = None
) -> list[str]:
    """Named shape errors of `governance.yaml`, the ones the render refuses. Unknown keys are
    left to the render, which reports them as a low finding. `exceptable` is the list of codes
    the last render accepts in an exception (None: not known, not checked)."""
    problems: list[str] = []
    for section in ("sources", "models", "policy"):
        problems += _is_mapping(section, document.get(section))
    problems += _is_list("exceptions", document.get("exceptions"))
    if problems:
        return problems
    for entry_id, entry in _mapping(document.get("sources")).items():
        problems += _check_source(str(entry_id), entry)
    for name, model in _mapping(document.get("models")).items():
        problems += _check_model(str(name), model)
    problems += _check_policy(_mapping(document.get("policy")))
    codes = None if exceptable is None else {*exceptable, *MANAGER_CODES}
    for index, item in enumerate(document.get("exceptions") or [], start=1):
        problems += _check_exception(index, item, codes)
    return problems


def _is_mapping(where: str, value: Any) -> list[str]:
    if value is None or isinstance(value, dict):
        return []
    return [_("'%(key)s' must be a mapping.") % {"key": where}]


def _is_list(where: str, value: Any) -> list[str]:
    if value is None or isinstance(value, list):
        return []
    return [_("'%(key)s' must be a list.") % {"key": where}]


def _is_text(where: str, value: Any) -> list[str]:
    if value is None or isinstance(value, str):
        return []
    return [_("'%(key)s' must be a text.") % {"key": where}]


def _enum(where: str, field: str, value: Any, allowed: Iterable[str]) -> list[str]:
    allowed = tuple(allowed)
    if value is None or value in allowed:
        return []
    return [_("%(where)s: %(field)s must be one of %(allowed)s.") % {
        "where": where, "field": field, "allowed": ", ".join(allowed)}]


def _check_source(entry_id: str, entry: Any) -> list[str]:
    if not _ID_RE.match(entry_id):
        return [_("sources: %(id)s is not a valid entry id.") % {"id": entry_id}]
    if entry is None:
        return []
    if not isinstance(entry, dict):
        return _is_mapping(f"sources.{entry_id}", entry)
    problems = _enum(entry_id, "classification", entry.get("classification"), CLASSES)
    problems += _enum(entry_id, "personal_data", entry.get("personal_data"), PERSONAL_DATA)
    for field in ("owner", "ropa_ref"):
        problems += _is_text(f"sources.{entry_id}.{field}", entry.get(field))
    subjects = entry.get("data_subjects")
    problems += _is_list(f"sources.{entry_id}.data_subjects", subjects)
    if isinstance(subjects, list):
        for subject in subjects:
            problems += _enum(entry_id, "data_subjects", subject, SUBJECTS)
    return problems


def _check_model(name: str, model: Any) -> list[str]:
    where = f"models.{name}"
    if not isinstance(model, dict) or model.get("tier") is None:
        return [_("%(where)s needs a tier.") % {"where": where}]
    return _enum(where, "tier", model.get("tier"), TIERS) + _is_text(
        f"{where}.role", model.get("role")
    )


def _check_policy(policy: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key, keys in (("allowed_tiers", CLASSES), ("personal_data_tiers", PERSONAL_DATA)):
        value = policy.get(key)
        problems += _is_mapping(f"policy.{key}", value)
        for sub, tiers in _mapping(value).items():
            problems += _enum(f"policy.{key}", "key", sub, keys)
            problems += _is_list(f"policy.{key}.{sub}", tiers)
            for tier in tiers if isinstance(tiers, list) else []:
                problems += _enum(f"policy.{key}.{sub}", "tier", tier, TIERS)
    roles = policy.get("broad_roles")
    problems += _is_list("policy.broad_roles", roles)
    for role in roles if isinstance(roles, list) else []:
        problems += _is_text("policy.broad_roles", role)
    problems += _is_text("policy.embedding_model", policy.get("embedding_model"))
    return problems


def _check_exception(index: int, item: Any, codes: set[str] | None) -> list[str]:
    where = _("exception %(n)s") % {"n": index}
    if not isinstance(item, dict):
        return [_("%(where)s is not a mapping.") % {"where": where}]
    problems = [
        _("%(where)s needs %(field)s.") % {"where": where, "field": field}
        for field in ("finding", "subject", "expires")
        if not item.get(field)
    ]
    for field in ("finding", "subject", "reason", "accepted_by_role", "risk_ref"):
        problems += _is_text(f"exceptions[{index}].{field}", item.get(field))
    if item.get("expires") and as_date(item["expires"]) is None:
        problems.append(_("%(where)s: %(field)s must be a date like 2026-11-14.")
                        % {"where": where, "field": "expires"})
    finding = item.get("finding")
    if codes is not None and isinstance(finding, str) and finding and finding not in codes:
        problems.append(_("%(where)s: %(finding)s is not a finding an exception can accept.")
                        % {"where": where, "finding": finding})
    return problems


# ── hosts and tiers ─────────────────────────────────────────────────────────────────────────


def host_is_internal(host: str, provider: str = "") -> bool:
    """Whether a deployment is served from inside the stack's own network: a private or
    loopback address, a bare service name, `localhost`, an internal suffix. A deployment
    without an address goes to its provider's public endpoint, unless the provider is one
    that runs on the host by default."""
    host = host.strip().lower().rstrip(".")
    if not host:
        return provider.lower() in _LOCAL_PROVIDERS
    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        return True
    with contextlib.suppress(ValueError):
        address = ipaddress.ip_address(host.strip("[]"))
        return address.is_private or address.is_loopback or address.is_link_local
    return "." not in host


def suggested_tier(deployments: Iterable[ModelEntry]) -> str | None:
    """`local` when every deployment is internal; otherwise the host says nothing reliable."""
    found = list(deployments)
    if found and all(host_is_internal(d.host, d.provider) for d in found):
        return "local"
    return None


@dataclass(frozen=True)
class LiveModels:
    """What LiteLLM serves, per deployment; `problem` when it could not be asked."""

    deployments: tuple[ModelEntry, ...] | None
    problem: str = ""

    @property
    def available(self) -> bool:
        return self.deployments is not None


async def read_live_models(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> LiveModels:
    """The deployments LiteLLM serves, or why they can't be read. Never raises."""
    secrets = litellm_secrets(settings.papaia_config_dir)
    if not secrets.master_key:
        return LiveModels(None, Problem(NO_KEY).text())
    client = LitellmClient(
        settings.litellm_url,
        secrets.master_key,
        verify=tls_verify(settings.ssl_cert_file),
        transport=transport,
    )
    try:
        data = await client.model_info()
    except LitellmError as exc:
        return LiveModels(None, str(exc))
    finally:
        await client.aclose()
    return LiveModels(parse_deployments(data))


def read_document(config_dir: str) -> tuple[dict[str, Any], km.FileState, str]:
    """(governance.yaml parsed, its state, why it could not be parsed). Raises
    `KnowledgeMapError` only when the file can't be read at all."""
    state = km.read_file(config_dir, "governance")
    try:
        return km._parsed(state), state, ""  # noqa: SLF001 - the one parser of operator files
    except km.KnowledgeMapError as exc:
        return {}, state, str(exc)


def lints_policy(rendered: dict[str, Any]) -> dict[str, Any]:
    """The effective policy as the last render evaluated it (defaults included)."""
    return _mapping(rendered.get("policy"))


def exceptable(rendered: dict[str, Any]) -> list[str]:
    """The codes an exception may name: the render's list and the manager's own."""
    return sorted({*(rendered.get("exceptable") or []), *MANAGER_CODES})


def live_findings(
    config_dir: str, live: LiveModels, today: date
) -> list[dict[str, Any]]:
    """The manager's own findings (models against LiteLLM), with active exceptions marked."""
    document, _state, _problem = read_document(config_dir)
    rendered = km.rendered(config_dir)
    _models, findings, _tiers = model_view(document, live, rendered)
    return apply_exceptions(findings, exceptions_view(document, today))


def _finding(code: str, severity: str, target: str, params: dict[str, Any]) -> dict[str, Any]:
    message, fix = describe(code, params) or (code, "")
    return {"severity": severity, "code": code, "target": target, "message": message,
            "origin": "manager", "category": "governance", "fix": fix, "params": params}


def _bare(name: Any) -> str:
    """A model name without the `model:` prefix older renders put in front of it."""
    text = str(name or "")
    return text.removeprefix("model:")


def model_view(
    document: dict[str, Any], live: LiveModels, rendered: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """(the model inventory, the manager's model findings, the tier each model is evaluated
    with). Which agent uses which model and the embedding model come from the render; without
    LiteLLM only those names and the ones in governance.yaml are known."""
    declared = _mapping(document.get("models"))
    embedding = str(lints_policy(rendered).get("embedding_model") or "")
    used_by: dict[str, list[str]] = {}
    for agent_id, agent in _mapping(rendered.get("agents")).items():
        name = str(_mapping(_mapping(agent).get("model")).get("name") or "")
        if name:
            used_by.setdefault(name, []).append(str(agent_id))
    # The render reports an unclassified model that is in use itself; the manager's finding
    # would say the same thing twice.
    reported = {
        _bare(f.get("target")) for f in rendered.get("findings") or []
        if isinstance(f, dict) and f.get("code") == "unclassified_model_in_use"
    }
    per_name: dict[str, list[ModelEntry]] = {}
    for deployment in live.deployments or ():
        per_name.setdefault(deployment.name, []).append(deployment)
    names = set(declared) | set(used_by) | set(per_name) | ({embedding} if embedding else set())
    models: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    tiers: dict[str, str] = {}
    for name in sorted(names, key=str.casefold):
        own = _mapping(declared.get(name))
        tier = str(own["tier"]) if own.get("tier") in TIERS else None
        deployments = per_name.get(name, [])
        internal = [host_is_internal(d.host, d.provider) for d in deployments]
        contradiction = tier == "local" and not all(internal)
        # Several deployments under one name: a request may land on any of them, so a name
        # listed as local but also served from a public host counts as the worst tier.
        effective = UNLISTED_TIER if contradiction else (tier or UNLISTED_TIER)
        tiers[name] = effective
        suggestion = suggested_tier(deployments)
        in_use = bool(used_by.get(name)) or name == embedding
        models.append({
            "name": name,
            "tier": tier,
            "role": str(own.get("role") or ""),
            "effective_tier": effective,
            "hosts": sorted({d.host for d in deployments if d.host}),
            "providers": sorted({d.provider for d in deployments if d.provider}),
            "mode": next((d.mode for d in deployments if d.mode), ""),
            "deployments": len(deployments),
            "in_litellm": bool(deployments),
            "mixed": len(set(internal)) > 1,
            "suggested_tier": suggestion,
            "used_by": used_by.get(name, []),
            "embedding": name == embedding,
        })
        if tier is None and name not in reported:
            findings.append(_finding(
                "unclassified_model", "high" if in_use else "medium", name,
                {"model": name, "suggested_tier": suggestion or "", "in_use": in_use}))
        if contradiction and deployments:
            public = sorted({
                d.host or d.provider
                for d, ok in zip(deployments, internal, strict=True) if not ok
            })
            findings.append(_finding(
                "model_tier_contradiction", "high", name, {"model": name, "hosts": public}))
    return models, findings, tiers


# ── the views ───────────────────────────────────────────────────────────────────────────────


def matrix(entries: list[dict[str, Any]], document: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per source and reference, and per server no source describes: what is
    documented, what is missing. A server that is only the access path of a source is
    covered by that source's row. Documentation only; the rules are the render's."""
    sources = _mapping(document.get("sources"))
    # The record-of-processing reference is only asked for once anybody uses it.
    ropa_used = any(_mapping(v).get("ropa_ref") for v in sources.values())
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("kind") not in ("source", "reference", "mcp"):
            continue
        only_a_path = entry.get("kind") == "mcp" and not entry.get("undeclared")
        if only_a_path and entry["id"] not in sources:
            continue
        own = _mapping(sources.get(entry["id"]))
        proposal = _mapping(entry.get("governance_proposal"))
        classification = own.get("classification") if own.get("classification") in CLASSES \
            else None
        data = str(own["personal_data"]) if own.get("personal_data") in PERSONAL_DATA \
            else "unknown"
        open_proposal = {
            k: v for k, v in proposal.items()
            if k in PROPOSAL_FIELDS and v not in (None, "", []) and own.get(k) is None
        }
        gaps = []
        if not own.get("owner"):
            gaps.append("owner")
        if classification is None:
            gaps.append("classification")
        if data == "unknown":
            gaps.append("personal_data")
        if ropa_used and data in DATA_WITH_RECORD and not own.get("ropa_ref"):
            gaps.append("ropa_ref")
        rows.append({
            "id": entry["id"],
            "system": entry.get("system") or entry["id"],
            "kind": entry.get("kind"),
            "declared_by": entry.get("declared_by") or "",
            "governance": jsonable({k: own[k] for k in SOURCE_FIELDS if k in own}),
            "proposal": jsonable(proposal),
            "open_proposal": jsonable(open_proposal),
            "classification": classification,
            "classification_confirmed": classification is not None,
            "personal_data": data,
            "data_subjects": _strings(own.get("data_subjects")),
            "owner": str(own.get("owner") or ""),
            "ropa_ref": str(own.get("ropa_ref") or ""),
            "required_roles": entry.get("required_roles") or [],
            "access": entry.get("access") or [],
            "gaps": gaps,
        })
    return rows


def agents_view(
    entries: list[dict[str, Any]], rendered: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every agent as the render evaluated it: model, tier, what it reaches and whether the
    policy allows the tier there."""
    names = {e["id"]: e.get("system") or e["id"] for e in entries}
    rows: list[dict[str, Any]] = []
    for agent_id, agent in sorted(_mapping(rendered.get("agents")).items()):
        agent = _mapping(agent)
        model = _mapping(agent.get("model"))
        result = agent.get("result")
        rows.append({
            "id": str(agent_id),
            "system": names.get(agent_id, str(agent_id)),
            "model": str(model.get("name") or ""),
            "provider": str(model.get("provider") or ""),
            "tier": agent.get("tier"),
            "tier_declared": bool(agent.get("tier_declared")),
            "reached": [{
                "id": str(r.get("entry")),
                "system": names.get(r.get("entry"), str(r.get("entry"))),
                "via": r.get("via"),
                "classification": r.get("classification"),
                "personal_data": r.get("personal_data"),
                "allowed": r.get("allowed"),
            } for r in agent.get("reached") or [] if isinstance(r, dict)],
            "result": result if result in ("ok", "attention", "not_assessable")
            else "not_assessable",
        })
    return rows


def _active(expires: Any, today: date) -> bool:
    """An exception holds until and including its `expires` day."""
    day = as_date(expires)
    return day is not None and today <= day


def exceptions_view(document: dict[str, Any], today: date) -> list[dict[str, Any]]:
    found = []
    for item in document.get("exceptions") or []:
        if isinstance(item, dict):
            found.append({**jsonable(item), "expired": not _active(item.get("expires"), today)})
    return found


def apply_exceptions(
    findings: list[dict[str, Any]], exceptions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mark a manager finding an active exception covers (the render marks its own)."""
    marked = []
    for finding in findings:
        match = next((x for x in exceptions if not x["expired"]
                      and x.get("finding") == finding["code"]
                      and x.get("subject") == finding["target"]), None)
        marked.append({**finding, "exception": match} if match else finding)
    return marked


def render_findings(findings: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """The governance findings of the last render, their exceptions checked against today:
    one that expired since the render no longer suppresses its finding, which then shows the
    lapsed exception instead."""
    return [as_of(f, today) for f in findings if f.get("category") == "governance"]


def as_of(finding: dict[str, Any], today: date) -> dict[str, Any]:
    """A render finding with its exception checked against today: the render marks it at
    render time, and an exception that has expired since no longer counts."""
    result: dict[str, Any] = jsonable(finding)
    exception = result.get("exception")
    if isinstance(exception, dict) and not _active(exception.get("expires"), today):
        result["expired_exception"] = result.pop("exception")
    return result


def sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Severity first, then the target."""
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(findings, key=lambda f: (order.get(str(f.get("severity")), len(order)),
                                           str(f.get("target") or ""), str(f.get("code"))))


# ── texts per finding code ──────────────────────────────────────────────────────────────────
#
# Every governance finding carries `params`, the structured values its message is made of, so
# the page says it in the reader's language. A text names the place in this interface where
# the change is made; a YAML path only where there is none. Unknown codes keep the message the
# render wrote.

_OPEN_ROLES_FIX = N_("Files → overrides.yaml: set required_roles of %(entry)s to the roles "
                     "that need it.")
_TEXTS: dict[str, tuple[str, str]] = {
    "classification_open": (
        N_("%(count)s entries are not yet classified."),
        N_("Governance → Sources: confirm the add-on proposal or set a classification with "
           "Edit, for %(entries)s."),
    ),
    "classification_downgraded": (
        N_("%(entry)s is classified %(classification)s, below the add-on's proposal "
           "%(proposed)s."),
        N_("Governance → Sources → Edit: set the classification of %(entry)s to %(proposed)s, "
           "or accept the finding with a reason."),
    ),
    "personal_data_unknown": (
        N_("%(entry)s: whether it holds personal data is not documented."),
        N_("Governance → Sources → Edit: set the personal data of %(entry)s."),
    ),
    "personal_data_without_ropa": (
        N_("%(entry)s holds %(personal_data)s personal data but has no record-of-processing "
           "reference."),
        N_("Governance → Sources → Edit: enter the record-of-processing reference of "
           "%(entry)s."),
    ),
    "open_confidential": (
        N_("%(entry)s (%(classification)s) is visible without required roles or to a broad "
           "role."),
        _OPEN_ROLES_FIX,
    ),
    "special_data_broad_access": (
        N_("%(entry)s holds %(personal_data)s personal data and is visible without required "
           "roles or to a broad role."),
        _OPEN_ROLES_FIX,
    ),
    "rag_copy_sensitive": (
        N_("%(entry)s: the vector index holds a copy of %(classification)s content that "
           "bypasses the source's rights and deletions."),
        N_("Take %(entry)s out of the vector index, or restrict its collection to the roles of "
           "the source and ingest it again after deletions."),
    ),
    "embedding_model_policy": (
        N_("%(entry)s: the embedding model %(model)s (tier %(tier)s) is not allowed for "
           "%(classification)s content with personal data %(personal_data)s."),
        N_("Set policy.embedding_model in governance.yaml to a model of tier "
           "%(allowed_tiers)s, or take %(entry)s out of the vector index."),
    ),
    "agent_model_policy": (
        N_("%(agent)s uses the model %(model)s (tier %(tier)s), which the policy does not "
           "allow for %(entries)s."),
        N_("Switch %(agent)s to a model of tier %(allowed_tiers)s and restrict its LiteLLM "
           "team or key to such models."),
    ),
    "unclassified_model_in_use": (
        N_("The model %(model)s has no tier and is evaluated as third_country."),
        N_("Governance → Models → Tier: set the tier of %(model)s."),
    ),
    "confidential_in_free_chat": (
        N_("%(server)s reaches %(entries)s and can be selected in free chat with any model."),
        N_("Set chatMenu: false for %(server)s in librechat.yaml and use it through agents "
           "whose model the policy allows."),
    ),
    "mcp_static_credentials": (
        N_("%(server)s uses a shared credential to reach %(entries)s; the target system no "
           "longer sees which user asks."),
        N_("Switch the MCP server %(server)s to the user's token instead of a shared "
           "credential."),
    ),
    "a2a_external": (
        N_("%(entry)s delegates to the external agent at %(host)s; what it receives is not "
           "documented."),
        N_("Check what %(host)s receives and under which contract, then record "
           "sources.%(entry)s (classification, personal_data) in governance.yaml."),
    ),
    "governance_unknown_key": (
        N_("governance.yaml: the unknown key %(key)s is ignored."),
        N_("Files → governance.yaml: remove %(key)s."),
    ),
    "governance_unknown_entry": (
        N_("governance.yaml describes %(entry)s, which is not on the map."),
        N_("Files → governance.yaml: remove sources.%(entry)s or correct the id."),
    ),
    "exception_expired": (
        N_("The accepted exception for %(finding)s on %(subject)s expired on %(expires)s."),
        N_("Fix the %(finding)s finding, or accept it again with “Accept until …” and a new "
           "date."),
    ),
    "unclassified_model": (
        N_("The model %(model)s has no tier in governance.yaml; it is evaluated as "
           "third_country."),
        N_("Governance → Models → Tier: set the tier of %(model)s."),
    ),
    "model_tier_contradiction": (
        N_("The model %(model)s is listed as local, but LiteLLM serves it from %(hosts)s."),
        N_("Either point every LiteLLM deployment of %(model)s at an internal host, or set its "
           "tier under Governance → Models → Tier to the one of %(hosts)s."),
    ),
}
# Variants a parameter selects; the message or fix replaces the one above.
_PROPOSED_COUNT = N_("%(count)s entries are not yet classified, %(proposed_count)s of them "
                     "with an add-on proposal.")
_UNKNOWN_IN_INDEX = N_("%(entry)s: whether it holds personal data is not documented; its "
                       "content is copied into the vector index.")
_ROPA_PROPOSED = N_("%(entry)s: the add-on suggests %(personal_data)s personal data; there is "
                    "no record-of-processing reference.")
_NOT_CLASSIFIED = N_("%(entry)s (%(classification)s, not yet classified) is visible without "
                     "required roles or to a broad role.")
_OR_CLASSIFY = N_("Files → overrides.yaml: set required_roles of %(entry)s to the roles that "
                  "need it, or classify it under Governance → Sources → Edit if it is less "
                  "sensitive.")
_RAG_PERSONAL = N_("%(entry)s: the vector index holds a copy of %(classification)s content "
                   "with %(personal_data)s personal data that bypasses the source's rights "
                   "and deletions.")
_USED_BY = N_("Used by %(agents)s.")
_EMBEDDING_FOR = N_("Embedding model for %(embedding_for)s.")
_CONFIRM_TIER = N_("Governance → Models: confirm the suggested tier %(suggested_tier)s for "
                   "%(model)s.")
_NONE = N_("none")


def _texts(code: str, params: dict[str, Any]) -> tuple[str, str] | None:
    """(message, fix) templates of a code, the variant the params select."""
    if code not in _TEXTS:
        return None
    message, fix = _TEXTS[code]
    if code == "classification_open" and params.get("proposed_count"):
        message = _PROPOSED_COUNT
    elif code == "personal_data_unknown" and params.get("rag"):
        message = _UNKNOWN_IN_INDEX
    elif code == "personal_data_without_ropa" and params.get("proposed"):
        message = _ROPA_PROPOSED
    elif code == "open_confidential" and params.get("classified") is False:
        message, fix = _NOT_CLASSIFIED, _OR_CLASSIFY
    elif code == "rag_copy_sensitive" and params.get("personal_data") in ("special", "criminal"):
        message = _RAG_PERSONAL
    elif code == "unclassified_model" and params.get("suggested_tier"):
        fix = _CONFIRM_TIER
    return message, fix


# Enum values as the reader sees them; the same message ids as the page's labels.
_VALUE_LABELS: dict[str, str] = {
    "public": N_("public"), "internal": N_("internal"), "confidential": N_("confidential"),
    "strictly_confidential": N_("strictly confidential"),
    "local": N_("local"), "eu_hosted": N_("EU-hosted"),
    "third_country_contracted": N_("third country, contracted"),
    "third_country": N_("third country"),
    "none": N_("no personal data"), "ordinary": N_("ordinary personal data"),
    "special": N_("special categories"), "criminal": N_("criminal offence data"),
}
# Params whose values are enum values; ids, names and hosts stay as they are.
_ENUM_PARAMS = frozenset({"classification", "proposed", "personal_data", "tier",
                          "allowed_tiers", "suggested_tier"})


def _label(key: str, value: Any) -> Any:
    if key in _ENUM_PARAMS and isinstance(value, str) and value in _VALUE_LABELS:
        return _(_VALUE_LABELS[value])
    return value


def _values(params: dict[str, Any]) -> dict[str, Any]:
    """The params as they go into a text: enum values as labels, lists joined, an empty list
    as `none`."""
    return {
        k: (", ".join(str(_label(k, x)) for x in v) or _(_NONE)) if isinstance(v, list)
        else _label(k, v)
        for k, v in params.items()
    }


def describe(code: str, params: dict[str, Any]) -> tuple[str, str] | None:
    """The message and fix of a finding in the reader's language, None when the code is not
    known here or its params don't fit."""
    texts = _texts(code, params)
    if texts is None:
        return None
    values = _values(params)
    try:
        message, fix = _(texts[0]) % values, _(texts[1]) % values
    except (KeyError, TypeError, ValueError):
        return None
    extra = [
        _(template) % values
        for key, template in (("agents", _USED_BY), ("embedding_for", _EMBEDDING_FOR))
        if code == "unclassified_model_in_use" and params.get(key)
    ]
    return " ".join([message, *extra]), fix


def localize(finding: dict[str, Any]) -> dict[str, Any]:
    """The finding with message and fix in the reader's language; the render's English text
    stays for codes this version doesn't know."""
    params = finding.get("params")
    texts = describe(str(finding.get("code")), params) if isinstance(params, dict) else None
    if texts is None:
        return finding
    return {**finding, "message": texts[0], "fix": texts[1]}


# ── edits ───────────────────────────────────────────────────────────────────────────────────


def _apply(target: dict[str, Any], changes: dict[str, Any]) -> None:
    """Set what has a value, remove what is empty (None, "", [])."""
    for key, value in changes.items():
        if value is None or value == "" or value == []:
            target.pop(key, None)
        else:
            target[key] = value


def set_entry(
    document: dict[str, Any], entry_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """The document with the governance of one entry changed."""
    result = copy.deepcopy(document)
    sources = result.get("sources")
    if not isinstance(sources, dict):
        sources = result["sources"] = {}
    entry = dict(_mapping(sources.get(entry_id)))
    _apply(entry, {k: v for k, v in changes.items() if k in SOURCE_FIELDS})
    if entry:
        sources[entry_id] = entry
    else:
        sources.pop(entry_id, None)
    return result


def confirm_proposal(
    document: dict[str, Any], entry_id: str, proposal: dict[str, Any]
) -> dict[str, Any]:
    """The document with the add-on's proposal taken over for every field the operator has
    not set. Raises when there is nothing to take over."""
    current = _mapping(_mapping(document.get("sources")).get(entry_id))
    taken = {k: copy.deepcopy(proposal[k]) for k in PROPOSAL_FIELDS
             if proposal.get(k) not in (None, "", []) and current.get(k) is None}
    if not taken:
        raise GovernanceError(_("There is no add-on proposal left to confirm for %(id)s.")
                              % {"id": entry_id})
    return set_entry(document, entry_id, taken)


def set_model(
    document: dict[str, Any], name: str, changes: dict[str, Any] | None
) -> dict[str, Any]:
    """The document with one model's tier changed; None removes it."""
    result = copy.deepcopy(document)
    models = result.get("models")
    if not isinstance(models, dict):
        models = result["models"] = {}
    if changes is None:
        models.pop(name, None)
        return result
    model = dict(_mapping(models.get(name)))
    _apply(model, {k: v for k, v in changes.items() if k in MODEL_FIELDS})
    models[name] = model
    return result


def set_exceptions(
    document: dict[str, Any], exceptions: list[dict[str, Any]]
) -> dict[str, Any]:
    result = copy.deepcopy(document)
    # One exception per (finding, subject): a renewal replaces the earlier one in its place.
    by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    for item in exceptions:
        cleaned = {k: v for k, v in item.items() if k in EXCEPTION_FIELDS and v not in (None, "")}
        key = (cleaned.get("finding"), cleaned.get("subject"))
        if key in by_key:
            by_key[key].clear()
            by_key[key].update(cleaned)
        else:
            by_key[key] = cleaned
    if by_key:
        result["exceptions"] = list(by_key.values())
    else:
        result.pop("exceptions", None)
    return result


# ── the fact export ─────────────────────────────────────────────────────────────────────────


def _roles(row: dict[str, Any]) -> list[str]:
    """The roles the entry itself requires."""
    return sorted({str(r) for r in row.get("required_roles") or []})


def _path_roles(row: dict[str, Any]) -> list[str]:
    """The roles each access path adds, as `via:target=role|role`."""
    found = []
    for path in row.get("access") or []:
        roles = sorted({str(r) for r in path.get("required_roles") or []})
        if roles:
            target = path.get("target") or path.get("location") or ""
            found.append(f"{path.get('via')}:{target}={'|'.join(roles)}")
    return found


def classification_open(findings: list[dict[str, Any]]) -> list[str]:
    """The entries the render's bundled reminder lists as not yet classified."""
    for finding in findings:
        if finding.get("code") == "classification_open" and finding.get("origin") != "manager":
            return _strings(finding.get("subjects"))
    return []


def _paths(row: dict[str, Any]) -> list[str]:
    return [f"{p.get('via')}:{p.get('target') or p.get('location') or ''}"
            for p in row.get("access") or []]


def facts(view: dict[str, Any], now: datetime, generator: str) -> dict[str, Any]:
    """Entries with stable ids, models with tiers, agents with their model, findings: for the
    operator's GRC, CMDB or wiki to import. Facts, no assessment."""
    open_ids = set(view.get("classification_open") or [])
    return {
        "kind": "papaia-knowledge-map-facts",
        "version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "generator": generator,
        "litellm_available": view["litellm"]["available"],
        "classification_open": sorted(open_ids),
        "entries": [{
            "id": r["id"], "name": r["system"], "kind": r["kind"],
            "declared_by": r["declared_by"], "owner": r["owner"],
            "classification": r["classification"],
            "classification_confirmed": r["classification_confirmed"],
            "personal_data": r["personal_data"], "data_subjects": r["data_subjects"],
            "ropa_ref": r["ropa_ref"], "roles": _roles(r), "path_roles": _path_roles(r),
            "paths": _paths(r), "classification_open": r["id"] in open_ids,
        } for r in view["matrix"]],
        "models": [{
            "name": m["name"], "tier": m["tier"], "effective_tier": m["effective_tier"],
            "suggested_tier": m["suggested_tier"], "role": m["role"], "hosts": m["hosts"],
            "in_litellm": m["in_litellm"], "used_by": m["used_by"],
            "embedding": m["embedding"],
        } for m in view["models"]],
        "agents": [{
            "id": a["id"], "name": a["system"], "model": a["model"], "tier": a["tier"],
            "tier_declared": a["tier_declared"],
            "reaches": [x["id"] for x in a["reached"]], "result": a["result"],
        } for a in view["agents"]],
        "findings": [{
            "code": f.get("code"), "severity": f.get("severity"), "target": f.get("target"),
            "origin": f.get("origin"), "message": f.get("message"), "fix": f.get("fix"),
            "params": f.get("params"), "exception": f.get("exception"),
            "expired_exception": f.get("expired_exception"),
        } for f in view["findings"]],
    }


def facts_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n"


def _cell(value: Any) -> str:
    """A CSV cell that a spreadsheet will not run as a formula."""
    if isinstance(value, list):
        value = "; ".join(str(v) for v in value)
    if isinstance(value, bool):
        value = "yes" if value else "no"
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def facts_csv(data: dict[str, Any]) -> str:
    """The entries of the fact export, one row each. Column names are stable keys."""
    columns = ("id", "name", "kind", "owner", "classification", "classification_confirmed",
               "classification_open", "personal_data", "data_subjects", "ropa_ref", "roles",
               "path_roles", "paths", "declared_by")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(columns)
    for entry in data["entries"]:
        writer.writerow([_cell(entry[c]) for c in columns])
    return out.getvalue()
