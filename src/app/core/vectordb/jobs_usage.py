"""Which ingest jobs write to which connection.

The ingester refuses to delete a connection a job uses, but only in its own web
interface. The manager is another writer of the same file, so it has to look for itself:
`jobs.yaml` next to `connections.yaml`, `jobs[*].target.connection`. `target` is not
part of `defaults`, so there is no inheritance to follow.

The scan is deliberately forgiving about the *shape* of a job (a disabled job, or one
the ingester would reject for another reason, still counts: it would write there again
once fixed) and strict about being unable to tell. A file that cannot be read or parsed
is reported as an error, and the caller treats that as "in use": deleting on a guess is
the one mistake that cannot be undone from the page.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

JOBS_RELPATH = Path("ai") / "rag" / "catalog" / "jobs.yaml"


@dataclass(frozen=True)
class JobsUsage:
    # Connection name -> ids of the jobs that target it, in file order.
    by_connection: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Why the answer is unknown, or None.
    error: str | None = None

    def jobs_of(self, name: str) -> tuple[str, ...]:
        return self.by_connection.get(name, ())


def jobs_using(config_dir: str | Path) -> JobsUsage:
    path = Path(config_dir) / JOBS_RELPATH
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return JobsUsage()
    except OSError as exc:
        return JobsUsage(error=f"jobs.yaml cannot be read ({exc.strerror or type(exc).__name__})")

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError:
        return JobsUsage(error="jobs.yaml is not valid YAML, so its jobs cannot be listed")
    if document is None:
        return JobsUsage()
    if not isinstance(document, dict):
        return JobsUsage(error="jobs.yaml does not hold a mapping, so its jobs cannot be listed")

    jobs = document.get("jobs")
    if jobs is None:
        return JobsUsage()
    if not isinstance(jobs, list):
        return JobsUsage(error="jobs in jobs.yaml is not a list, so its jobs cannot be listed")

    used: dict[str, list[str]] = {}
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            continue
        target = job.get("target")
        connection = target.get("connection") if isinstance(target, dict) else None
        if not isinstance(connection, str) or not connection:
            continue
        job_id = job.get("id")
        label = job_id if isinstance(job_id, str) and job_id else f"jobs[{index}]"
        used.setdefault(connection, []).append(label)
    return JobsUsage(by_connection={name: tuple(ids) for name, ids in used.items()})
