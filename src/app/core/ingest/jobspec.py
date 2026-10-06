"""The ingester's job schema, as far as the manager's pages need to know it.

The ingester validates `jobs.yaml` and is the last word on it. This module is a mirror of its
schema (`catalog/schema.py`, `catalog/loader.py`) that exists so a page can say *which field*
is wrong before anything is written, and so the editor knows what to render. It never decides
alone: after a write the manager still asks the ingester whether it took the job, and a
difference between the two is shown as the ingester's answer.

Everything here works on the *authored* mapping, the way the file holds a job: a secret is a
`${env:QI_SECRET_X}` reference, the webdav/sftp/smb/ftp password is the key `pass`, and a job
that keeps a default does not mention it. That is deliberate. The ingester's own models hold
the *resolved* job (a secret becomes the variable name, the catalog `defaults:` are merged
in), and writing that back would produce a file that no longer loads.

Two ideas carry the rest:

* **Effective versus authored.** A job inherits sections from the catalog's `defaults:`. The
  editor shows the effective values (so an administrator sees what will actually happen),
  and `minimise` writes back only what differs from what the job would inherit anyway. A
  value equal to the *catalog* default is left out, so changing that default later moves the
  job with it; a value equal only to the *schema* default but different from the catalog
  default is kept, because it is an override. The identity of a job (`source`, `target`,
  `mode`) and its embedding model are never left out: a job that merely inherited its model
  would silently switch it when somebody edits the defaults.
* **The mirror is checked against the real thing.** `tests/test_ingester_contract.py` loads
  jobs written by this module with the ingester's own loader when its source is available.
"""
from __future__ import annotations

import copy
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.functional_validators import AfterValidator

from app.core.ingest import schedules
from app.core.schedule import ScheduleError, zone

SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
COLLECTION_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"
_SLUG_RE = re.compile(SLUG_PATTERN)

SECRET_PREFIX = "QI_SECRET_"
_REF_RE = re.compile(r"^\$\{env:(QI_SECRET_[A-Z0-9_]+)\}$")
_NAME_RE = re.compile(r"^QI_SECRET_[A-Z0-9_]+$")

# Payload keys owned by the ingestion contract.
RESERVED_PAYLOAD_KEYS = frozenset({"text", "source", "ingest_job", "ingest_run", "acl_tags"})

# Sections of `defaults:` that are mixed under every job.
DEFAULT_SECTIONS: tuple[str, ...] = ("embedding", "chunking", "filters", "schedule", "safety")

LOCAL_ROOT = "/data/local"

# Ids that would be taken for a page of the manager or for the managed jobs.
RESERVED_IDS = frozenset({"new"})
MANAGED_PREFIX = "mgr-"

MODES: tuple[str, ...] = ("append", "upsert", "full")
CHUNK_STRATEGIES: tuple[str, ...] = ("auto", "markdown", "paragraph", "sheet_rows", "slide")


def secret_ref(name: str) -> str:
    """The authored form of a reference to a credential."""
    return f"${{env:{name}}}"


def secret_name(value: Any) -> str | None:
    """The credential name inside a `${env:NAME}` reference, or None for anything else."""
    if not isinstance(value, str):
        return None
    match = _REF_RE.match(value.strip())
    return match.group(1) if match else None


def _check_secret(value: str) -> str:
    if _REF_RE.match(value.strip()) is None:
        raise ValueError(
            "choose a stored credential; a value typed here would end up in jobs.yaml "
            "(references look like ${env:QI_SECRET_NAME})"
        )
    return value.strip()


SecretRef = Annotated[str, AfterValidator(_check_secret)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class _Source(_Strict):
    # Authored keys of the secret fields, in the form the file uses (`pass`, not `password`).
    secret_keys: ClassVar[frozenset[str]] = frozenset()

    label: str = Field(pattern=SLUG_PATTERN)
    rclone_flags: list[str] = Field(default_factory=list)


class LocalSource(_Source):
    type: Literal["local"]
    path: str = Field(min_length=1)


class S3Source(_Source):
    secret_keys = frozenset({"access_key_id", "secret_access_key"})

    type: Literal["s3"]
    bucket: str = Field(min_length=1)
    prefix: str = ""
    provider: str = "AWS"
    region: str = ""
    endpoint: str = ""
    access_key_id: SecretRef | None = None
    secret_access_key: SecretRef | None = None


class WebdavSource(_Source):
    secret_keys = frozenset({"pass"})

    type: Literal["webdav"]
    url: str = Field(min_length=1)
    vendor: str = "other"
    user: str = ""
    password: SecretRef | None = Field(default=None, alias="pass")


class SftpSource(_Source):
    secret_keys = frozenset({"pass", "key_file"})

    type: Literal["sftp"]
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = ""
    password: SecretRef | None = Field(default=None, alias="pass")
    key_file: SecretRef | None = None
    path: str = "/"


class SmbSource(_Source):
    secret_keys = frozenset({"pass"})

    type: Literal["smb"]
    host: str = Field(min_length=1)
    share: str = Field(min_length=1)
    user: str = ""
    password: SecretRef | None = Field(default=None, alias="pass")
    path: str = ""


class FtpSource(_Source):
    secret_keys = frozenset({"pass"})

    type: Literal["ftp"]
    host: str = Field(min_length=1)
    port: int = Field(default=21, ge=1, le=65535)
    user: str = ""
    password: SecretRef | None = Field(default=None, alias="pass")
    path: str = ""
    tls: bool = False


class GdriveSource(_Source):
    secret_keys = frozenset({"service_account_json", "token"})

    type: Literal["gdrive"]
    service_account_json: SecretRef | None = None
    token: SecretRef | None = None
    root_folder_id: str = ""


class AzureBlobSource(_Source):
    secret_keys = frozenset({"key", "sas_url"})

    type: Literal["azureblob"]
    account: str = Field(min_length=1)
    container: str = Field(min_length=1)
    key: SecretRef | None = None
    sas_url: SecretRef | None = None
    prefix: str = ""


class HttpSource(_Source):
    type: Literal["http"]
    url: str = Field(min_length=1)


SourceSpec = Annotated[
    LocalSource
    | S3Source
    | WebdavSource
    | SftpSource
    | SmbSource
    | FtpSource
    | GdriveSource
    | AzureBlobSource
    | HttpSource,
    Field(discriminator="type"),
]

_SOURCE_MODELS: dict[str, type[_Source]] = {
    "local": LocalSource,
    "s3": S3Source,
    "webdav": WebdavSource,
    "sftp": SftpSource,
    "smb": SmbSource,
    "ftp": FtpSource,
    "gdrive": GdriveSource,
    "azureblob": AzureBlobSource,
    "http": HttpSource,
}
SOURCE_TYPES: tuple[str, ...] = tuple(_SOURCE_MODELS)


def secret_keys_of(source_type: str) -> frozenset[str]:
    model = _SOURCE_MODELS.get(source_type)
    return model.secret_keys if model is not None else frozenset()


def _source_defaults(source_type: str) -> dict[str, Any]:
    """The value of every optional key of a source type, by its authored key."""
    model = _SOURCE_MODELS[source_type]
    out: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if name in ("type", "label") or info.is_required():
            continue
        key = info.alias or name
        out[key] = info.get_default(call_default_factory=True)
    return out


# ---------------------------------------------------------------------------
# The rest of a job
# ---------------------------------------------------------------------------


class FiltersSpec(_Strict):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    max_file_bytes: int | None = Field(default=None, ge=1)


class TargetSpec(_Strict):
    collection: str = Field(pattern=COLLECTION_PATTERN)
    connection: str = Field(pattern=SLUG_PATTERN)
    acl_tags: list[str] = Field(default_factory=list)
    extra_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra_payload")
    @classmethod
    def _no_reserved_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        clashes = RESERVED_PAYLOAD_KEYS.intersection(value)
        if clashes:
            raise ValueError(
                "extra payload may not use the keys the ingester owns: "
                + ", ".join(sorted(clashes))
            )
        return value


class ScheduleSpec(_Strict):
    cron: str | None = None
    every: str | None = None
    timezone: str | None = None
    jitter_seconds: int = Field(default=30, ge=0)
    misfire_grace_seconds: int = Field(default=300, ge=0)
    run_on_startup: Literal["never", "if_missed", "always"] = "if_missed"

    @model_validator(mode="after")
    def _cron_xor_every(self) -> ScheduleSpec:
        if self.cron is not None and self.every is not None:
            raise ValueError("a schedule takes either a cron expression or an interval, not both")
        if self.every is not None:
            problem = schedules.every_error(self.every)
            if problem:
                raise ValueError(problem)
        return self


class ChunkingSpec(_Strict):
    strategy: Literal["auto", "markdown", "paragraph", "sheet_rows", "slide"] = "auto"
    words: int = Field(default=400, ge=1)
    overlap: int = Field(default=50, ge=0)

    @model_validator(mode="after")
    def _overlap_below_words(self) -> ChunkingSpec:
        if self.overlap >= self.words:
            raise ValueError("the overlap must be smaller than the chunk size")
        return self


class EmbeddingSpec(_Strict):
    model: str | None = None
    batch_size: int | None = Field(default=None, ge=1)


class SafetySpec(_Strict):
    max_delete_ratio: float = Field(default=0.25, ge=0.0, le=1.0)
    empty_source_guard: bool = True


class JobSpec(_Strict):
    id: str = Field(pattern=SLUG_PATTERN)
    enabled: bool = True
    description: str = ""
    source: SourceSpec
    filters: FiltersSpec = Field(default_factory=FiltersSpec)
    target: TargetSpec
    mode: Literal["full", "append", "upsert"]
    full_scope: Literal["job", "collection"] = "job"
    append_probe: Literal["auto", "state", "qdrant"] = "auto"
    schedule: ScheduleSpec = Field(default_factory=ScheduleSpec)
    chunking: ChunkingSpec = Field(default_factory=ChunkingSpec)
    embedding: EmbeddingSpec = Field(default_factory=EmbeddingSpec)
    safety: SafetySpec = Field(default_factory=SafetySpec)
    mcp_allow_full: bool = False
    expand_embedded: bool = False
    source_template: str = "{scheme}://{label}/{rel_path}"

    @field_validator("source_template")
    @classmethod
    def _placeholders(cls, value: str) -> str:
        if "{rel_path}" not in value or "{label}" not in value:
            raise ValueError("the source template must contain {label} and {rel_path}")
        return value


_SECTION_MODELS: dict[str, type[_Strict]] = {
    "filters": FiltersSpec,
    "schedule": ScheduleSpec,
    "chunking": ChunkingSpec,
    "embedding": EmbeddingSpec,
    "safety": SafetySpec,
}

# Top-level keys of a job with a default of their own, and what it is.
TOP_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "description": "",
    "full_scope": "job",
    "append_probe": "auto",
    "mcp_allow_full": False,
    "expand_embedded": False,
    "source_template": "{scheme}://{label}/{rel_path}",
}


def section_defaults(section: str) -> dict[str, Any]:
    """What the ingester assumes for a section when neither the job nor the catalog says."""
    return _SECTION_MODELS[section]().model_dump()


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """One problem, attributable to a job and a field, in the ingester's dotted form."""

    job_id: str | None
    field: str
    message: str

    def __str__(self) -> str:
        scope = f"{self.job_id}: " if self.job_id else ""
        return f"{scope}{self.field}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "field": self.field, "message": self.message}


_FRIENDLY: dict[str, str] = {
    "missing": "this is required",
    "extra_forbidden": "is not a setting the ingester knows",
    "string_too_short": "this is required",
    "string_pattern_mismatch": "has a format the ingester does not accept",
    "int_parsing": "must be a whole number",
    "float_parsing": "must be a number",
    "bool_parsing": "must be true or false",
    "greater_than_equal": "is too small",
    "less_than_equal": "is too large",
}


def _issue_from(job_id: str | None, error: Mapping[str, Any]) -> Issue:
    loc = [str(part) for part in error["loc"]]
    # The union of sources names the chosen type inside the location (`source.s3.bucket`);
    # the ingester's own report does not, and the editor addresses fields without it.
    if len(loc) >= 2 and loc[0] == "source" and loc[1] in _SOURCE_MODELS:
        loc = [loc[0], *loc[2:]]
    path = ".".join(loc) or "<root>"
    kind = str(error.get("type"))
    text = str(error.get("msg", "is invalid"))
    if kind == "string_pattern_mismatch":
        tail = path.rsplit(".", 1)[-1]
        if tail in ("id", "label", "connection"):
            text = (
                "use lowercase letters, digits, '-' and '_' (up to 64, starting with a letter "
                "or a digit)"
            )
        elif tail == "collection":
            text = (
                "use letters, digits, '.', '_' and '-' (up to 128, starting with a letter "
                "or a digit)"
            )
        else:
            text = _FRIENDLY[kind]
    elif kind in _FRIENDLY:
        text = _FRIENDLY[kind]
    elif kind == "value_error":
        text = text.removeprefix("Value error, ")
    elif kind == "literal_error" and loc and loc[-1] == "type":
        text = "choose one of the source types"
    return Issue(job_id, path, text)


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """The ingester's merge: dictionaries merge, everything else is replaced by the job's value."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def known_defaults(defaults: Mapping[str, Any] | None) -> dict[str, Any]:
    """The part of a catalog's `defaults:` the ingester applies."""
    if not isinstance(defaults, Mapping):
        return {}
    return {key: copy.deepcopy(defaults[key]) for key in DEFAULT_SECTIONS if key in defaults}


def effective(raw: Mapping[str, Any], defaults: Mapping[str, Any] | None) -> dict[str, Any]:
    """The job as the ingester merges it: the catalog defaults under the authored keys."""
    return deep_merge(known_defaults(defaults), raw)


def parse_job(
    raw: Mapping[str, Any], defaults: Mapping[str, Any] | None = None, *, label: str | None = None
) -> tuple[JobSpec | None, list[Issue]]:
    """Validate an authored job against the schema, with the catalog defaults merged in."""
    raw_id = raw.get("id")
    job_id = raw_id if isinstance(raw_id, str) and raw_id else label
    try:
        spec = JobSpec.model_validate(effective(raw, defaults))
    except ValidationError as exc:
        return None, [_issue_from(job_id, error) for error in exc.errors()]
    return spec, []


# ---------------------------------------------------------------------------
# Writing a job back: only what differs from what it would inherit
# ---------------------------------------------------------------------------


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def inherited_sections(defaults: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Per section, what a job gets for a key it does not set: catalog default, else schema."""
    chosen = known_defaults(defaults)
    return {
        section: deep_merge(section_defaults(section), chosen.get(section) or {})
        for section in DEFAULT_SECTIONS
    }


def minimise(job: Mapping[str, Any], defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The mapping to write for an edited job: everything it needs, nothing it inherits.

    Secret fields given as a bare name are written as a `${env:NAME}` reference. Input that
    is not a valid job is returned as far as it goes; validation is the caller's job.
    """
    out: dict[str, Any] = {"id": job.get("id")}
    if job.get("enabled") is False:
        out["enabled"] = False
    description = str(job.get("description") or "").strip()
    if description:
        out["description"] = description

    source_in = job.get("source")
    out["source"] = _minimise_source(source_in if isinstance(source_in, Mapping) else {})

    target_in = job.get("target")
    target = target_in if isinstance(target_in, Mapping) else {}
    out_target: dict[str, Any] = {
        "collection": target.get("collection"),
        "connection": target.get("connection"),
    }
    if target.get("acl_tags"):
        tags = [str(tag).strip() for tag in target["acl_tags"] if str(tag).strip()]
        if tags:
            out_target["acl_tags"] = tags
    if target.get("extra_payload"):
        out_target["extra_payload"] = copy.deepcopy(dict(target["extra_payload"]))
    out["target"] = out_target

    out["mode"] = job.get("mode")
    for key in TOP_DEFAULTS:
        if key in ("enabled", "description"):
            continue
        value = job.get(key)
        if value is not None and value != TOP_DEFAULTS[key]:
            out[key] = value

    inherited = inherited_sections(defaults)
    for section in DEFAULT_SECTIONS:
        section_in = job.get(section)
        if not isinstance(section_in, Mapping):
            continue
        kept: dict[str, Any] = {}
        for key, value in section_in.items():
            if section == "embedding" and key == "model":
                if not _empty(value):
                    kept[key] = value
                continue
            if _empty(value) and inherited[section].get(key) in (None, "", [], {}):
                continue
            if key in inherited[section] and value == inherited[section][key]:
                continue
            kept[key] = value
        if kept:
            out[section] = kept
    return out


def _as_reference(value: str) -> str:
    """`QI_SECRET_X` becomes `${env:QI_SECRET_X}`; a reference stays one.

    Anything else is returned as typed, so the schema check can refuse it by name instead of
    the page silently rewriting it into something that looks valid.
    """
    if secret_name(value) is not None:
        return value
    return secret_ref(value) if _NAME_RE.match(value) else value


def _minimise_source(source: Mapping[str, Any]) -> dict[str, Any]:
    source_type = str(source.get("type") or "")
    out: dict[str, Any] = {"type": source_type, "label": source.get("label")}
    if source_type not in _SOURCE_MODELS:
        return out
    defaults = _source_defaults(source_type)
    secrets = secret_keys_of(source_type)
    model = _SOURCE_MODELS[source_type]
    required = {
        (info.alias or name)
        for name, info in model.model_fields.items()
        if info.is_required() and name not in ("type", "label")
    }
    for key, value in source.items():
        if key in ("type", "label"):
            continue
        if key in secrets and isinstance(value, str) and value.strip():
            out[key] = _as_reference(value.strip())
            continue
        if key in required:
            out[key] = value
            continue
        if _empty(value) or (key in defaults and value == defaults[key]):
            continue
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# The rules of the loader that need more than the schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleContext:
    """What the ingester knows when it loads a catalog and a page has to ask for.

    `connections` and `secrets` are None when they cannot be read; the rule is then not
    checked here, and the ingester has the last word.
    """

    connections: frozenset[str] | None = None
    secrets: frozenset[str] | None = None
    system_collections: frozenset[str] = frozenset({"_collection_meta", "_rbac_acl"})
    local_root: str = LOCAL_ROOT


def _local_path_problem(path: str, root: str) -> str | None:
    mount = posixpath.normpath(root.replace("\\", "/")).rstrip("/")
    normalised = posixpath.normpath(path.replace("\\", "/"))
    if normalised != mount and not normalised.startswith(mount + "/"):
        return f"local source paths must live under '{mount}' (got '{path}')"
    return None


def check_job(job_id: str, spec: JobSpec, ctx: RuleContext) -> list[Issue]:
    """The per-job rules of the ingester's loader that the schema does not carry."""
    issues: list[Issue] = []
    schedule = spec.schedule
    if schedule.cron is not None:
        problem = schedules.cron_error(schedule.cron)
        if problem:
            issues.append(Issue(job_id, "schedule.cron", problem))
    if schedule.timezone:
        # The ingester does not check the zone when it loads a catalog; an unknown one fails
        # later, when the job is scheduled. Refusing it here keeps it out of the file.
        try:
            zone(schedule.timezone)
        except ScheduleError as exc:
            issues.append(Issue(job_id, "schedule.timezone", str(exc)))
    if ctx.secrets is not None:
        for key in sorted(spec.source.secret_keys):
            value = spec.source.model_dump(by_alias=True).get(key)
            name = secret_name(value)
            if name is not None and name not in ctx.secrets:
                issues.append(
                    Issue(
                        job_id,
                        f"source.{key}",
                        f"the credential '{name}' does not exist; add it on the Credentials "
                        "page or choose another",
                    )
                )
    if isinstance(spec.source, LocalSource):
        problem = _local_path_problem(spec.source.path, ctx.local_root)
        if problem:
            issues.append(Issue(job_id, "source.path", problem))
    if spec.enabled and not spec.embedding.model:
        issues.append(
            Issue(
                job_id,
                "embedding.model",
                "no embedding model: set one for this job or in the catalog defaults",
            )
        )
    if ctx.connections is not None and spec.target.connection not in ctx.connections:
        issues.append(
            Issue(
                job_id,
                "target.connection",
                f"unknown connection '{spec.target.connection}'; add it on the Connections page",
            )
        )
    return issues


def _cross_job(specs: Iterable[tuple[str, JobSpec]], ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    ordered = list(specs)
    seen: set[str] = set()
    for job_id, _ in ordered:
        if job_id in seen:
            issues.append(Issue(job_id, "id", "duplicate job id"))
        seen.add(job_id)
    for job_id, spec in ordered:
        if spec.target.collection in ctx.system_collections:
            issues.append(
                Issue(
                    job_id,
                    "target.collection",
                    f"'{spec.target.collection}' is a system collection and cannot be a target",
                )
            )
    enabled = [(job_id, spec) for job_id, spec in ordered if spec.enabled]

    labels: dict[str, dict[str, str]] = {}
    for job_id, spec in enabled:
        by_label = labels.setdefault(spec.target.collection, {})
        other = by_label.get(spec.source.label)
        if other is not None:
            issues.append(
                Issue(
                    job_id,
                    "source.label",
                    f"the label '{spec.source.label}' already serves the collection "
                    f"'{spec.target.collection}' in the job '{other}'; labels have to differ "
                    "per collection",
                )
            )
        else:
            by_label[spec.source.label] = job_id

    models: dict[str, tuple[str, str]] = {}
    connections: dict[str, tuple[str, str]] = {}
    for job_id, spec in enabled:
        model = spec.embedding.model
        if model:
            seen_model = models.get(spec.target.collection)
            if seen_model is not None and seen_model[0] != model:
                issues.append(
                    Issue(
                        job_id,
                        "embedding.model",
                        f"the collection '{spec.target.collection}' is already served with "
                        f"'{seen_model[0]}' by the job '{seen_model[1]}'; one model per collection",
                    )
                )
            elif seen_model is None:
                models[spec.target.collection] = (model, job_id)
        connection = spec.target.connection
        seen_connection = connections.get(spec.target.collection)
        if seen_connection is not None and seen_connection[0] != connection:
            issues.append(
                Issue(
                    job_id,
                    "target.connection",
                    f"the collection '{spec.target.collection}' is already served over "
                    f"'{seen_connection[0]}' by the job '{seen_connection[1]}'; one connection "
                    "per collection",
                )
            )
        elif seen_connection is None:
            connections[spec.target.collection] = (connection, job_id)
    return issues


def check_catalog(document: Mapping[str, Any], ctx: RuleContext) -> list[Issue]:
    """What the ingester would say about a whole catalog: schema, per-job and cross-job rules."""
    issues: list[Issue] = []
    version = document.get("version")
    if version != 1:
        return [Issue(None, "version", f"unsupported catalog version {version!r}; expected 1")]
    defaults = document.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        return [Issue(None, "defaults", "defaults must be a mapping")]
    unknown = set(defaults) - set(DEFAULT_SECTIONS)
    if unknown:
        issues.append(
            Issue(None, "defaults", "unsupported defaults sections: " + ", ".join(sorted(unknown)))
        )
    jobs = document.get("jobs") or []
    if not isinstance(jobs, list):
        return [*issues, Issue(None, "jobs", "jobs must be a list")]
    valid: list[tuple[str, JobSpec]] = []
    for index, raw in enumerate(jobs):
        if not isinstance(raw, Mapping):
            issues.append(Issue(None, f"jobs[{index}]", "job must be a mapping"))
            continue
        spec, problems = parse_job(raw, defaults, label=f"jobs[{index}]")
        issues.extend(problems)
        if spec is None:
            continue
        issues.extend(check_job(spec.id, spec, ctx))
        valid.append((spec.id, spec))
    issues.extend(_cross_job(valid, ctx))
    return issues


def check_one(document: Mapping[str, Any], job_id: str, ctx: RuleContext) -> list[Issue]:
    """The problems that concern one job: its own, and the ones it causes with its neighbours."""
    return [issue for issue in check_catalog(document, ctx) if issue.job_id == job_id]


# ---------------------------------------------------------------------------
# What the editor renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldForm:
    """One input of a source's form."""

    key: str
    label: str
    kind: Literal["text", "integer", "bool", "secret", "folder", "select", "url"] = "text"
    required: bool = False
    default: Any = ""
    help: str = ""
    placeholder: str = ""
    options: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "default": self.default,
            "help": self.help,
            "placeholder": self.placeholder,
            "options": list(self.options),
        }


@dataclass(frozen=True)
class SourceForm:
    type: str
    title: str
    blurb: str
    fields: tuple[FieldForm, ...] = field(default=())
    # Needs the ingester to fetch the files first; a local folder is read in place.
    remote: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "title": self.title,
            "blurb": self.blurb,
            "remote": self.remote,
            "fields": [item.as_dict() for item in self.fields],
        }


_VENDORS = (
    "other",
    "nextcloud",
    "owncloud",
    "sharepoint",
    "sharepoint-ntlm",
    "fastmail",
    "rclone",
)

SOURCE_FORMS: dict[str, SourceForm] = {
    "local": SourceForm(
        "local",
        "Folder on the server",
        "A folder in the documents area. Read in place, nothing is copied.",
        (
            FieldForm(
                "path",
                "Folder",
                "folder",
                True,
                help="Below /data/local, which is the documents folder of the RAG module.",
                placeholder="/data/local/handbook",
            ),
        ),
        remote=False,
    ),
    "s3": SourceForm(
        "s3",
        "S3 bucket",
        "Amazon S3 or any S3-compatible store (MinIO, Wasabi, Ceph ...).",
        (
            FieldForm("bucket", "Bucket", "text", True, placeholder="acme-reports"),
            FieldForm("prefix", "Folder inside the bucket", "text", placeholder="published/"),
            FieldForm("provider", "Provider", "text", default="AWS",
                      help="AWS, Minio, Wasabi, Ceph, Other ...; the name rclone uses."),
            FieldForm("region", "Region", "text", placeholder="eu-central-1"),
            FieldForm("endpoint", "Endpoint", "url", help="Only for a store that is not AWS.",
                      placeholder="https://s3.example.com"),
            FieldForm("access_key_id", "Access key ID", "secret"),
            FieldForm("secret_access_key", "Secret access key", "secret"),
        ),
    ),
    "webdav": SourceForm(
        "webdav",
        "WebDAV",
        "Nextcloud, ownCloud, SharePoint and other WebDAV servers.",
        (
            FieldForm("url", "URL", "url", True,
                      placeholder="https://cloud.example.com/remote.php/dav/files/me/Docs"),
            FieldForm("vendor", "Server type", "select", default="other", options=_VENDORS),
            FieldForm("user", "User name", "text"),
            FieldForm("pass", "Password", "secret"),
        ),
    ),
    "sftp": SourceForm(
        "sftp",
        "SFTP",
        "A directory on a server reached over SSH.",
        (
            FieldForm("host", "Host", "text", True, placeholder="sftp.example.com"),
            FieldForm("port", "Port", "integer", default=22),
            FieldForm("user", "User name", "text"),
            FieldForm("pass", "Password", "secret"),
            FieldForm("key_file", "Private key", "secret",
                      help="The key itself (PEM text) as a credential, not a path."),
            FieldForm("path", "Directory", "text", default="/", placeholder="/export/legal"),
        ),
    ),
    "smb": SourceForm(
        "smb",
        "SMB share",
        "A Windows or Samba file share.",
        (
            FieldForm("host", "Host", "text", True, placeholder="files.example.com"),
            FieldForm("share", "Share", "text", True, placeholder="documents"),
            FieldForm("user", "User name", "text"),
            FieldForm("pass", "Password", "secret"),
            FieldForm("path", "Folder in the share", "text"),
        ),
    ),
    "ftp": SourceForm(
        "ftp",
        "FTP",
        "A directory on an FTP server.",
        (
            FieldForm("host", "Host", "text", True, placeholder="ftp.example.com"),
            FieldForm("port", "Port", "integer", default=21),
            FieldForm("user", "User name", "text"),
            FieldForm("pass", "Password", "secret"),
            FieldForm("path", "Directory", "text"),
            FieldForm("tls", "Use explicit TLS", "bool", default=False),
        ),
    ),
    "gdrive": SourceForm(
        "gdrive",
        "Google Drive",
        "A Google Drive, read with a service account or a token.",
        (
            FieldForm("service_account_json", "Service account (JSON)", "secret",
                      help="The JSON of the service account as a credential."),
            FieldForm("token", "OAuth token", "secret"),
            FieldForm("root_folder_id", "Root folder ID", "text",
                      help="Limits the job to one folder. Leave empty for the whole drive."),
        ),
    ),
    "azureblob": SourceForm(
        "azureblob",
        "Azure Blob Storage",
        "A container in an Azure storage account.",
        (
            FieldForm("account", "Storage account", "text", True),
            FieldForm("container", "Container", "text", True),
            FieldForm("key", "Account key", "secret"),
            FieldForm("sas_url", "SAS URL", "secret"),
            FieldForm("prefix", "Folder in the container", "text"),
        ),
    ),
    "http": SourceForm(
        "http",
        "Web directory",
        "A web server that lists its files (an Apache or nginx index).",
        (FieldForm("url", "URL", "url", True, placeholder="https://files.example.com/docs/"),),
    ),
}


def source_forms() -> list[dict[str, Any]]:
    return [form.as_dict() for form in SOURCE_FORMS.values()]


def form_keys(source_type: str) -> set[str]:
    return {item.key for item in SOURCE_FORMS[source_type].fields}


def model_keys(source_type: str) -> set[str]:
    """The authored keys of a source type, without `type` and `label`."""
    model = _SOURCE_MODELS[source_type]
    return {
        (info.alias or name)
        for name, info in model.model_fields.items()
        if name not in ("type", "label")
    }


def is_valid_id(value: str) -> bool:
    return _SLUG_RE.fullmatch(value) is not None


def id_problem(value: str) -> str | None:
    """Why an id cannot be used for a new job, or None."""
    if not is_valid_id(value):
        return (
            "use lowercase letters, digits, '-' and '_' (up to 64, starting with a letter "
            "or a digit)"
        )
    if value in RESERVED_IDS:
        return f"'{value}' is reserved"
    if value.startswith(MANAGED_PREFIX):
        return (
            f"ids starting with '{MANAGED_PREFIX}' belong to the Embedding page; choose another"
        )
    return None

