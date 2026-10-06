"""Between the job editor and `jobs.yaml`: the editor's state, and the entry it becomes.

The editor holds a job as one JSON object (the *form state*) with every field present and a
plain value in it: lists are lists, the schedule is the builder's choices, and a credential
is the bare name `QI_SECRET_X`. `jobs.yaml` holds the *authored* mapping, which leaves out
everything that is as it would be anyway. This module converts both ways:

* `form_state(raw, defaults)` for opening a job: the effective values, so the editor shows
  what will actually happen (a chunk size from the catalog defaults is shown as the chunk
  size, not as an empty field);
* `authored(state, defaults)` for saving: the entry to write, minimal, plus the problems
  found on the way (an extra payload that is not JSON, a schedule that cannot run).

Neither does any I/O.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from app.core.ingest import jobspec, schedules
from app.core.ingest.jobspec import Issue
from app.core.schedule import ScheduleError

# The modes in the words of the editor: what the job does with a source over time.
MODE_CHOICES: tuple[dict[str, str], ...] = (
    {
        "value": "append",
        "title": "Add new files only",
        "text": (
            "Files that are new in the source are added. A file that changed is left as it "
            "was embedded, and a file that disappeared stays in the collection."
        ),
    },
    {
        "value": "upsert",
        "title": "Keep in sync",
        "text": (
            "New files are added, changed files replace their old content and files that "
            "disappeared from the source are removed from the collection. A safety limit "
            "stops a run that would remove too much."
        ),
    },
    {
        "value": "full",
        "title": "Rebuild every time",
        "text": (
            "Everything of this job is embedded again on every run and what was not "
            "refreshed is removed afterwards. Slow and costly; for sources that change "
            "completely."
        ),
    },
)

STRATEGY_TEXT = {
    "auto": "Choose by file type",
    "markdown": "By headings (Markdown)",
    "paragraph": "By paragraphs",
    "sheet_rows": "By rows (spreadsheets)",
    "slide": "By slide (presentations)",
}

STARTUP_TEXT = {
    "if_missed": "Run once if a scheduled run was missed",
    "never": "Never",
    "always": "Always run when the ingester starts",
}

# Filter presets: the include patterns of the file types people ask for by name.
FILTER_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "title": "Documents",
        "include": ["**/*.pdf", "**/*.docx", "**/*.doc", "**/*.odt", "**/*.rtf"],
    },
    {"title": "Text and Markdown", "include": ["**/*.md", "**/*.txt", "**/*.rst"]},
    {"title": "Spreadsheets", "include": ["**/*.xlsx", "**/*.xls", "**/*.ods", "**/*.csv"]},
    {"title": "Presentations", "include": ["**/*.pptx", "**/*.ppt", "**/*.odp"]},
    {"title": "Web pages", "include": ["**/*.html", "**/*.htm"]},
)


def source_detail(source: Mapping[str, Any]) -> str:
    """Where the files come from, in one short line for a list."""
    kind = str(source.get("type") or "")

    def part(key: str) -> str:
        value = source.get(key)
        return str(value).strip("/") if isinstance(value, str) else ""

    if kind == "local":
        return str(source.get("path") or "")
    if kind == "s3":
        tail = f"/{part('prefix')}" if part("prefix") else ""
        return f"s3://{part('bucket')}{tail}"
    if kind in ("webdav", "http"):
        return str(source.get("url") or "")
    if kind == "sftp":
        user = f"{source['user']}@" if source.get("user") else ""
        return f"sftp://{user}{source.get('host', '')}{source.get('path') or '/'}"
    if kind == "smb":
        tail = f"/{part('path')}" if part("path") else ""
        return f"//{source.get('host', '')}/{part('share')}{tail}"
    if kind == "ftp":
        return f"ftp://{source.get('host', '')}/{part('path')}".rstrip("/")
    if kind == "gdrive":
        folder = part("root_folder_id")
        return f"Google Drive, folder {folder}" if folder else "Google Drive"
    if kind == "azureblob":
        tail = f"/{part('prefix')}" if part("prefix") else ""
        return f"{part('account')}/{part('container')}{tail}"
    return ""


def source_title(source_type: str) -> str:
    form = jobspec.SOURCE_FORMS.get(source_type)
    return form.title if form else source_type


# ---------------------------------------------------------------------------
# Opening a job
# ---------------------------------------------------------------------------


def _bare(value: Any) -> Any:
    """A `${env:NAME}` reference as `NAME`, for the editor's credential picker."""
    name = jobspec.secret_name(value)
    return name if name is not None else value


def blank_state(defaults: Mapping[str, Any] | None, *, timezone: str) -> dict[str, Any]:
    """A new job: the effective defaults, one local source, nothing chosen yet."""
    return form_state(
        {
            "id": "",
            "source": {"type": "local", "label": "", "path": ""},
            "target": {"collection": "", "connection": "default"},
            "mode": "upsert",
        },
        defaults,
        timezone=timezone,
    )


def form_state(
    raw: Mapping[str, Any], defaults: Mapping[str, Any] | None, *, timezone: str = "UTC"
) -> dict[str, Any]:
    """The editor's state for an authored job, with the catalog defaults applied."""
    merged = jobspec.effective(raw, defaults)
    inherited = jobspec.inherited_sections(defaults)

    def section(name: str) -> dict[str, Any]:
        value = merged.get(name)
        return jobspec.deep_merge(inherited[name], value if isinstance(value, Mapping) else {})

    source_in = raw.get("source")
    source = dict(source_in) if isinstance(source_in, Mapping) else {}
    source_type = str(source.get("type") or "local")
    source_state: dict[str, Any] = {
        "type": source_type,
        "label": source.get("label") or "",
        "rclone_flags": list(source.get("rclone_flags") or []),
    }
    for key in jobspec.form_keys(source_type) if source_type in jobspec.SOURCE_FORMS else ():
        value = _bare(source.get(key))
        form = next(f for f in jobspec.SOURCE_FORMS[source_type].fields if f.key == key)
        source_state[key] = form.default if value is None else value

    target_in = raw.get("target")
    target = target_in if isinstance(target_in, Mapping) else {}
    schedule_state = asdict(schedules.read_plan(section("schedule")))
    schedule_state.pop("extra", None)

    filters = section("filters")
    chunking = section("chunking")
    embedding = section("embedding")
    safety = section("safety")
    return {
        "id": raw.get("id") or "",
        "enabled": raw.get("enabled", True) is not False,
        "description": raw.get("description") or "",
        "source": source_state,
        "target": {
            "collection": target.get("collection") or "",
            "connection": target.get("connection") or "default",
            "acl_tags": list(target.get("acl_tags") or []),
            "extra_payload": json.dumps(target.get("extra_payload"), indent=2)
            if target.get("extra_payload")
            else "",
        },
        "mode": raw.get("mode") or "upsert",
        "full_scope": raw.get("full_scope") or "job",
        "append_probe": raw.get("append_probe") or "auto",
        "filters": {
            "include": list(filters.get("include") or []),
            "exclude": list(filters.get("exclude") or []),
            "max_file_bytes": filters.get("max_file_bytes"),
        },
        "schedule": {**schedule_state, "timezone": schedule_state.get("timezone") or ""},
        "chunking": {
            "strategy": chunking.get("strategy"),
            "words": chunking.get("words"),
            "overlap": chunking.get("overlap"),
        },
        "embedding": {
            "model": embedding.get("model") or "",
            "batch_size": embedding.get("batch_size"),
        },
        "safety": {
            "max_delete_ratio": safety.get("max_delete_ratio"),
            "empty_source_guard": safety.get("empty_source_guard"),
        },
        "mcp_allow_full": bool(raw.get("mcp_allow_full", False)),
        "expand_embedded": bool(raw.get("expand_embedded", False)),
        "source_template": raw.get("source_template") or jobspec.TOP_DEFAULTS["source_template"],
        "ingest_timezone": timezone,
    }


# ---------------------------------------------------------------------------
# Saving a job
# ---------------------------------------------------------------------------


def _int_or_none(value: Any) -> int | None | str:
    """A whole number, None for an empty field, or the text itself so the schema can refuse it."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return str(value)


def _number_or_none(value: Any) -> float | int | None | str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip())
    except ValueError:
        return str(value)


def _lines(value: Any) -> list[str]:
    """A list the editor may hand over as a list or as text, one entry per line."""
    if isinstance(value, str):
        items = [line.strip() for line in value.splitlines()]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        return []
    return [item for item in items if item]


def authored(
    state: Mapping[str, Any], defaults: Mapping[str, Any] | None
) -> tuple[dict[str, Any], list[Issue]]:
    """The entry to write for the editor's state, and the problems found converting it.

    A problem here is one the schema could not express (a payload that is not JSON, a
    schedule that cannot run). The result is still returned, as far as it goes, so the
    caller can run the schema over it and report everything at once.
    """
    issues: list[Issue] = []
    job_id = state.get("id") if isinstance(state.get("id"), str) else None

    source_in = state.get("source")
    source = dict(source_in) if isinstance(source_in, Mapping) else {}
    source_type = str(source.get("type") or "")
    if source_type in jobspec.SOURCE_FORMS:
        for form in jobspec.SOURCE_FORMS[source_type].fields:
            if form.kind == "integer" and form.key in source:
                source[form.key] = _int_or_none(source[form.key])
            if form.kind == "bool" and form.key in source:
                source[form.key] = bool(source[form.key])
    source["rclone_flags"] = _lines(source.get("rclone_flags"))
    for key in ("label",):
        if isinstance(source.get(key), str):
            source[key] = source[key].strip()

    target_in = state.get("target")
    target_state = target_in if isinstance(target_in, Mapping) else {}
    payload: Any = target_state.get("extra_payload")
    extra: dict[str, Any] = {}
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            issues.append(Issue(job_id, "target.extra_payload", f"is not valid JSON: {exc.msg}"))
        else:
            if isinstance(parsed, dict):
                extra = parsed
            else:
                issues.append(Issue(job_id, "target.extra_payload", "must be a JSON object"))
    elif isinstance(payload, dict):
        extra = payload
    target = {
        "collection": str(target_state.get("collection") or "").strip(),
        "connection": str(target_state.get("connection") or "").strip(),
        "acl_tags": _lines(target_state.get("acl_tags")),
        "extra_payload": extra,
    }

    filters_in = state.get("filters")
    filters_state = filters_in if isinstance(filters_in, Mapping) else {}
    filters = {
        "include": _lines(filters_state.get("include")),
        "exclude": _lines(filters_state.get("exclude")),
        "max_file_bytes": _int_or_none(filters_state.get("max_file_bytes")),
    }

    schedule: dict[str, Any] = {}
    schedule_in = state.get("schedule")
    if isinstance(schedule_in, Mapping):
        try:
            plan = _plan_from(schedule_in)
            schedule = schedules.schedule_block(plan, full=True)
        except (ScheduleError, ValueError, TypeError) as exc:
            issues.append(Issue(job_id, "schedule", str(exc)))

    chunking_in = state.get("chunking")
    chunking_state = chunking_in if isinstance(chunking_in, Mapping) else {}
    chunking = {
        "strategy": chunking_state.get("strategy"),
        "words": _int_or_none(chunking_state.get("words")),
        "overlap": _int_or_none(chunking_state.get("overlap")),
    }
    embedding_in = state.get("embedding")
    embedding_state = embedding_in if isinstance(embedding_in, Mapping) else {}
    embedding = {
        "model": str(embedding_state.get("model") or "").strip() or None,
        "batch_size": _int_or_none(embedding_state.get("batch_size")),
    }
    safety_in = state.get("safety")
    safety_state = safety_in if isinstance(safety_in, Mapping) else {}
    safety = {
        "max_delete_ratio": _number_or_none(safety_state.get("max_delete_ratio")),
        "empty_source_guard": safety_state.get("empty_source_guard"),
    }

    flat: dict[str, Any] = {
        "id": str(state.get("id") or "").strip(),
        "enabled": state.get("enabled", True) is not False,
        "description": state.get("description") or "",
        "source": source,
        "target": target,
        "mode": state.get("mode"),
        "full_scope": state.get("full_scope"),
        "append_probe": state.get("append_probe"),
        "mcp_allow_full": bool(state.get("mcp_allow_full", False)),
        "expand_embedded": bool(state.get("expand_embedded", False)),
        "source_template": state.get("source_template"),
        "filters": filters,
        "schedule": schedule,
        "chunking": {k: v for k, v in chunking.items() if v is not None},
        "embedding": {k: v for k, v in embedding.items() if v is not None},
        "safety": {k: v for k, v in safety.items() if v is not None},
    }
    return jobspec.minimise(flat, defaults), issues


def _plan_from(raw: Mapping[str, Any]) -> schedules.Plan:
    mode = str(raw.get("mode") or "manual")
    if mode not in schedules.MODES:
        raise ScheduleError(f"Unknown schedule type {mode!r}.")
    weekdays = raw.get("weekdays") or ()
    return schedules.Plan(
        mode=mode,  # type: ignore[arg-type]
        every_n=_as_int(raw.get("every_n"), 15),
        every_unit=str(raw.get("every_unit") or "m"),
        time=str(raw.get("time") or "03:00"),
        minute=_as_int(raw.get("minute"), 0),
        weekdays=tuple(str(day) for day in weekdays),
        day_of_month=_as_int(raw.get("day_of_month"), 1),
        cron=str(raw.get("cron") or ""),
        timezone=(str(raw.get("timezone") or "").strip() or None),
        jitter_seconds=_as_int(raw.get("jitter_seconds"), 30),
        misfire_grace_seconds=_as_int(raw.get("misfire_grace_seconds"), 300),
        run_on_startup=str(raw.get("run_on_startup") or "if_missed"),
    )


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        raise ValueError("a number is expected")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if value is None or value == "":
        return default
    raise ValueError("a whole number is expected")


def plan_from(raw: Mapping[str, Any]) -> schedules.Plan:
    """The schedule builder's plan from the editor's JSON (for the live preview)."""
    return _plan_from(raw)
