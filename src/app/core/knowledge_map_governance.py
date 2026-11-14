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

The core's render validates the file and writes its findings into `lints.yaml`
(`category: governance`, each with a `fix`), plus the add-on proposals (`proposals`). The
manager adds what only it can see, the models LiteLLM actually serves (`origin: manager`).
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
EXCEPTION_FIELDS = ("finding", "subject", "expires", "reason", "risk_ref")

DEFAULT_POLICY: dict[str, Any] = {
    "allowed_tiers": {
        "public": list(TIERS),
        "internal": ["local", "eu_hosted", "third_country_contracted"],
        "confidential": ["local", "eu_hosted"],
        "strictly_confidential": ["local"],
    },
    "personal_data_tiers": {"special": ["local"], "criminal": ["local"]},
    "broad_roles": ["user"],
    "embedding_model": "",
}

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


def class_rank(value: str | None) -> int:
    """Order of strictness; anything that is not a class ranks as confidential."""
    if value not in CLASSES:
        return CLASSES.index("confidential")
    return CLASSES.index(str(value))


# ── the shape check ─────────────────────────────────────────────────────────────────────────


def check_document(document: dict[str, Any]) -> list[str]:
    """Named shape errors of `governance.yaml`, the ones the render refuses. Unknown keys are
    left to the render, which reports them as a low finding."""
    problems: list[str] = []
    for section in ("sources", "models", "policy"):
        if document.get(section) is not None and not isinstance(document[section], dict):
            problems.append(_("'%(key)s' must be a mapping.") % {"key": section})
    if document.get("exceptions") is not None and not isinstance(document["exceptions"], list):
        problems.append(_("'%(key)s' must be a list.") % {"key": "exceptions"})
    if problems:
        return problems
    for entry_id, entry in _mapping(document.get("sources")).items():
        problems += _check_source(str(entry_id), entry)
    for name, model in _mapping(document.get("models")).items():
        problems += _check_model(str(name), model)
    problems += _check_policy(_mapping(document.get("policy")))
    for index, item in enumerate(document.get("exceptions") or [], start=1):
        problems += _check_exception(index, item)
    return problems


def _enum(where: str, field: str, value: Any, allowed: Iterable[str]) -> list[str]:
    allowed = tuple(allowed)
    if value is None or value in allowed:
        return []
    return [_("%(where)s: %(field)s must be one of %(allowed)s.") % {
        "where": where, "field": field, "allowed": ", ".join(allowed)}]


def _check_source(entry_id: str, entry: Any) -> list[str]:
    if not _ID_RE.match(entry_id):
        return [_("sources: %(id)s is not a valid entry id.") % {"id": entry_id}]
    if not isinstance(entry, dict):
        return [_("The changes for %(id)s are not a mapping.") % {"id": entry_id}]
    problems = _enum(entry_id, "classification", entry.get("classification"), CLASSES)
    problems += _enum(entry_id, "personal_data", entry.get("personal_data"), PERSONAL_DATA)
    subjects = entry.get("data_subjects")
    if subjects is not None:
        if not isinstance(subjects, list):
            problems.append(_("%(where)s: %(field)s must be a list.")
                            % {"where": entry_id, "field": "data_subjects"})
        else:
            for subject in subjects:
                problems += _enum(entry_id, "data_subjects", subject, SUBJECTS)
    return problems


def _check_model(name: str, model: Any) -> list[str]:
    where = f"models.{name}"
    if not isinstance(model, dict) or model.get("tier") is None:
        return [_("%(where)s needs a tier.") % {"where": where}]
    return _enum(where, "tier", model.get("tier"), TIERS)


def _check_policy(policy: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key, keys in (("allowed_tiers", CLASSES), ("personal_data_tiers", PERSONAL_DATA)):
        for sub, tiers in _mapping(policy.get(key)).items():
            problems += _enum(f"policy.{key}", "key", sub, keys)
            for tier in tiers if isinstance(tiers, list) else [tiers]:
                problems += _enum(f"policy.{key}.{sub}", "tier", tier, TIERS)
    roles = policy.get("broad_roles")
    if roles is not None and not (
        isinstance(roles, list) and all(isinstance(r, str) for r in roles)
    ):
        problems.append(_("%(where)s: %(field)s must be a list.")
                        % {"where": "policy", "field": "broad_roles"})
    return problems


def _check_exception(index: int, item: Any) -> list[str]:
    where = _("exception %(n)s") % {"n": index}
    if not isinstance(item, dict):
        return [_("%(where)s is not a mapping.") % {"where": where}]
    problems = [
        _("%(where)s needs %(field)s.") % {"where": where, "field": field}
        for field in ("finding", "subject", "expires")
        if not item.get(field)
    ]
    if item.get("expires") and as_date(item["expires"]) is None:
        problems.append(_("%(where)s: %(field)s must be a date like 2026-11-14.")
                        % {"where": where, "field": "expires"})
    return problems


# ── the policy ──────────────────────────────────────────────────────────────────────────────


def policy_of(document: dict[str, Any]) -> dict[str, Any]:
    """The policy with every key the operator left out at its default."""
    merged = copy.deepcopy(DEFAULT_POLICY)
    own = _mapping(document.get("policy"))
    for key in ("allowed_tiers", "personal_data_tiers"):
        merged[key].update(_mapping(own.get(key)))
    if isinstance(own.get("broad_roles"), list):
        merged["broad_roles"] = _strings(own["broad_roles"])
    if own.get("embedding_model"):
        merged["embedding_model"] = str(own["embedding_model"])
    return merged


def tier_allowed(
    tier: str, classification: str, personal_data: str, policy: dict[str, Any]
) -> bool:
    allowed = _strings(policy["allowed_tiers"].get(CLASSES[class_rank(classification)]))
    if tier not in allowed:
        return False
    data_tiers = policy["personal_data_tiers"].get(personal_data)
    return data_tiers is None or tier in _strings(data_tiers)


def effective_class(governance: dict[str, Any], proposal: dict[str, Any]) -> str:
    """The class the checks use: the operator's; for an unclassified entry the stricter of the
    add-on's proposal and `internal` (an add-on cannot lower it to public), without a proposal
    `confidential`."""
    own = governance.get("classification")
    if own in CLASSES:
        return str(own)
    proposed = proposal.get("classification")
    if proposed in CLASSES:
        return CLASSES[max(class_rank(proposed), CLASSES.index("internal"))]
    return "confidential"


def effective_personal_data(governance: dict[str, Any]) -> str:
    """The operator's value; until then `unknown` (a proposal is never effective)."""
    value = governance.get("personal_data")
    return str(value) if value in PERSONAL_DATA else "unknown"


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


def live_findings(
    config_dir: str, live: LiveModels, today: date
) -> list[dict[str, Any]]:
    """The manager's own findings (models against LiteLLM), with valid exceptions marked."""
    document, _state, _problem = read_document(config_dir)
    agents = [e for e in km.rendered(config_dir)["entries"] if e.get("kind") == "agent"]
    _models, findings, _tiers = model_view(document, live, agents)
    valid = [x for x in exceptions_view(document, today) if not x["expired"]]
    return apply_exceptions(findings, valid)


def _finding(code: str, severity: str, target: str, message: str, fix: str) -> dict[str, Any]:
    return {"severity": severity, "code": code, "target": target, "message": message,
            "origin": "manager", "category": "governance", "fix": fix}


def model_view(
    document: dict[str, Any], live: LiveModels, agents: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """(the model inventory, the manager's model findings, the tier each model is evaluated
    with). Without LiteLLM only the names from governance.yaml and the agents are known."""
    declared = _mapping(document.get("models"))
    embedding = str(policy_of(document)["embedding_model"] or "")
    used_by: dict[str, list[str]] = {}
    for agent in agents:
        name = str(_mapping(agent.get("model")).get("name") or "")
        if name:
            used_by.setdefault(name, []).append(str(agent["id"]))
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
        if tier is None:
            fix = (
                _("Confirm the suggested tier %(tier)s for %(name)s on the Governance tab.")
                % {"tier": suggestion, "name": name}
                if suggestion
                else _("Set the tier of %(name)s under models in governance.yaml (local, "
                       "eu_hosted, third_country_contracted or third_country).")
                % {"name": name}
            )
            findings.append(_finding(
                "unclassified_model", "high" if in_use else "medium", name,
                _("The model %(name)s has no tier in governance.yaml; it is evaluated as "
                  "third_country.") % {"name": name}, fix))
        if contradiction and deployments:
            public = ", ".join(sorted({
                d.host or d.provider
                for d, ok in zip(deployments, internal, strict=True) if not ok
            }))
            findings.append(_finding(
                "model_tier_contradiction", "high", name,
                _("The model %(name)s is listed as local, but LiteLLM serves it from %(hosts)s.")
                % {"name": name, "hosts": public},
                _("Either point every LiteLLM deployment of %(name)s at an internal host, or "
                  "set its tier to the one of %(hosts)s.")
                % {"name": name, "hosts": public}))
    return models, findings, tiers


# ── the views ───────────────────────────────────────────────────────────────────────────────


def matrix(entries: list[dict[str, Any]], document: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per source and reference, and per server no source describes: what is
    documented, what is missing. A server that is only the access path of a source is
    covered by that source's row."""
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
        data = effective_personal_data(own)
        open_proposal = {
            k: v for k, v in proposal.items()
            if k in PROPOSAL_FIELDS and v not in (None, "", []) and own.get(k) is None
        }
        gaps = []
        if not own.get("owner"):
            gaps.append("owner")
        if own.get("classification") not in CLASSES:
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
            "classification": effective_class(own, proposal),
            "classification_confirmed": own.get("classification") in CLASSES,
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
    entries: list[dict[str, Any]], document: dict[str, Any], tiers: dict[str, str]
) -> list[dict[str, Any]]:
    """Every agent with its model's tier and what it reaches, and whether the policy allows
    that tier for each reached entry."""
    policy = policy_of(document)
    sources = _mapping(document.get("sources"))
    routable = [e for e in entries if e.get("kind") in ("source", "reference")]
    rows: list[dict[str, Any]] = []
    for agent in entries:
        if agent.get("kind") != "agent":
            continue
        name = str(_mapping(agent.get("model")).get("name") or "")
        tier = tiers.get(name, UNLISTED_TIER) if name else UNLISTED_TIER
        uses = set(agent.get("uses") or [])
        reached: list[dict[str, Any]] = []
        for entry in routable:
            for path in entry.get("access") or []:
                if path.get("via") in ("mcp", "rag") and path.get("target") in uses:
                    own = _mapping(sources.get(entry["id"]))
                    cls = effective_class(own, _mapping(entry.get("governance_proposal")))
                    data = effective_personal_data(own)
                    reached.append({
                        "id": entry["id"], "system": entry.get("system") or entry["id"],
                        "via": path["via"], "classification": cls, "personal_data": data,
                        "allowed": tier_allowed(tier, cls, data, policy),
                    })
        rows.append({
            "id": agent["id"],
            "system": agent.get("system") or agent["id"],
            "model": name,
            "tier": tier,
            "tier_declared": name in _mapping(document.get("models")),
            "reached": reached,
            "result": "ok" if all(r["allowed"] for r in reached) else "action_needed",
        })
    return rows


def exceptions_view(document: dict[str, Any], today: date) -> list[dict[str, Any]]:
    found = []
    for item in document.get("exceptions") or []:
        if isinstance(item, dict):
            expires = as_date(item.get("expires"))
            found.append({**jsonable(item), "expired": bool(expires and expires < today)})
    return found


def apply_exceptions(
    findings: list[dict[str, Any]], exceptions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mark a manager finding a valid exception covers (the render marks its own)."""
    marked = []
    for finding in findings:
        match = next((x for x in exceptions if x.get("finding") == finding["code"]
                      and x.get("subject") == finding["target"]), None)
        marked.append({**finding, "exception": match} if match else finding)
    return marked


def render_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The governance findings of the last render."""
    return [jsonable(f) for f in findings if f.get("category") == "governance"]


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
    cleaned = [{k: v for k, v in x.items() if k in EXCEPTION_FIELDS and v not in (None, "")}
               for x in exceptions]
    if cleaned:
        result["exceptions"] = cleaned
    else:
        result.pop("exceptions", None)
    return result


# ── the fact export ─────────────────────────────────────────────────────────────────────────


def _roles(row: dict[str, Any]) -> list[str]:
    roles = set(row.get("required_roles") or [])
    for path in row.get("access") or []:
        roles |= set(path.get("required_roles") or [])
    return sorted(roles)


def _paths(row: dict[str, Any]) -> list[str]:
    return [f"{p.get('via')}:{p.get('target') or p.get('location') or ''}"
            for p in row.get("access") or []]


def facts(view: dict[str, Any], now: datetime, generator: str) -> dict[str, Any]:
    """Entries with stable ids, models with tiers, agents with their model, findings: for the
    operator's GRC, CMDB or wiki to import. Facts, no assessment."""
    return {
        "kind": "papaia-knowledge-map-facts",
        "version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "generator": generator,
        "litellm_available": view["litellm"]["available"],
        "entries": [{
            "id": r["id"], "name": r["system"], "kind": r["kind"],
            "declared_by": r["declared_by"], "owner": r["owner"],
            "classification": r["classification"],
            "classification_confirmed": r["classification_confirmed"],
            "personal_data": r["personal_data"], "data_subjects": r["data_subjects"],
            "ropa_ref": r["ropa_ref"], "roles": _roles(r), "paths": _paths(r),
        } for r in view["matrix"]],
        "models": [{
            "name": m["name"], "tier": m["tier"], "effective_tier": m["effective_tier"],
            "suggested_tier": m["suggested_tier"], "role": m["role"], "hosts": m["hosts"],
            "in_litellm": m["in_litellm"], "used_by": m["used_by"],
            "embedding": m["embedding"],
        } for m in view["models"]],
        "agents": [{
            "id": a["id"], "name": a["system"], "model": a["model"], "tier": a["tier"],
            "reaches": [x["id"] for x in a["reached"]], "result": a["result"],
        } for a in view["agents"]],
        "findings": [{
            "code": f.get("code"), "severity": f.get("severity"), "target": f.get("target"),
            "origin": f.get("origin"), "message": f.get("message"), "fix": f.get("fix"),
            "exception": f.get("exception"),
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
               "personal_data", "data_subjects", "ropa_ref", "roles", "paths", "declared_by")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(columns)
    for entry in data["entries"]:
        writer.writerow([_cell(entry[c]) for c in columns])
    return out.getvalue()
