"""Making the ingester read a catalog change, and checking that it took it.

The ingester keeps its previous catalog when the new one has an invalid job anywhere in it,
so "the job exists afterwards" proves nothing: it may be the definition of an earlier write.
Every writer of `jobs.yaml` in the manager therefore does the same three things after a
write: ask for a reload, read what the ingester reports about the job, and compare what it
serves with what was written. This module is that, shared by the Embedding page and the
job management.

What the answer means differs by caller, which is why `reload_and_verify` has two modes:

* the Embedding page writes a job it is about to run, so anything short of "served as
  written" is a failure and the write is rolled back;
* a person editing the catalog may be saving a perfectly good job into a file in which
  another job is broken. The ingester then refuses the whole catalog, and the right
  answer is to keep the write and say that it is not active yet and why, not to undo a
  correct change because of somebody else's mistake.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.ingest.client import IngestClient
from app.core.ingest.errors import CatalogRejected

# Keys of the source whose value the ingester masks when it reports a job.
_SECRET_FIELDS = frozenset(
    {"pass", "access_key_id", "secret_access_key", "key_file", "service_account_json",
     "token", "key", "sas_url"}
)


@dataclass(frozen=True)
class LoadOutcome:
    """What the ingester did with the catalog after a write."""

    # True when the ingester serves the written definition.
    applied: bool
    # Problems of other jobs (or of the file) that stop the ingester from applying anything.
    elsewhere: tuple[str, ...] = ()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def issue_text(item: dict[str, Any]) -> str:
    who = item.get("job_id") or "jobs.yaml"
    return f"{who}: {item.get('field')}: {item.get('message')}"


async def reload_and_verify(
    client: IngestClient,
    job_id: str,
    matches: Callable[[dict[str, Any]], bool],
    *,
    tolerate_other_problems: bool = False,
) -> LoadOutcome:
    """Make the ingester read the catalog now and check it serves what was written.

    `matches` receives the job as the ingester reports it (`GET /v1/jobs/{id}`) and says
    whether it is the definition that was written. Raises `CatalogRejected` when the
    ingester refuses the job, or (unless `tolerate_other_problems`) the catalog.
    """
    info = await client.reload()
    errors = [item for item in info.get("errors") or [] if isinstance(item, dict)]
    mine = [issue_text(item) for item in errors if item.get("job_id") == job_id]
    detail = await client.job(job_id)
    if not mine and detail is not None and matches(detail):
        return LoadOutcome(True)
    others = tuple(issue_text(item) for item in errors if item.get("job_id") != job_id)
    if not mine and others and tolerate_other_problems:
        return LoadOutcome(False, others)
    problems = mine or list(others) or ["the ingester kept its previous job catalog"]
    raise CatalogRejected(
        "The ingester did not take the job: " + "; ".join(problems), tuple(problems)
    )


def subset_matches(wanted: Any, served: Any) -> bool:
    """Whether everything in `wanted` is in `served`, with the same values.

    The ingester reports a job with every default filled in, so what the manager wrote is
    a subset of what it serves. Comparing only the keys that were written means a field the
    manager does not know about, or a default that changes, does not look like a rejection.
    """
    if isinstance(wanted, dict):
        if not isinstance(served, dict):
            return False
        return all(
            key in served and subset_matches(value, served[key]) for key, value in wanted.items()
        )
    return bool(wanted == served)


def loaded_as_written(detail: dict[str, Any], effective_job: dict[str, Any]) -> bool:
    """Whether the ingester serves `effective_job` (the job with the catalog defaults merged in).

    Source secrets are skipped: the ingester reports them masked, so only their presence
    can be compared.
    """
    config = detail.get("config")
    if not isinstance(config, dict):
        return False
    wanted = dict(effective_job)
    wanted_source = dict(_mapping(wanted.get("source")))
    served_source = _mapping(config.get("source"))
    for key in list(wanted_source):
        if key in _SECRET_FIELDS:
            if not served_source.get(key):
                return False
            del wanted_source[key]
    wanted["source"] = wanted_source
    return subset_matches(wanted, config)
