"""What can go wrong when a file is embedded through the ingester, as the page tells it.

Each class maps to one answer of the API: `InvalidRequest` to 422, `Conflict` to 409,
`NotFound` to 404, `IngestUnavailable` to 503 and `IngestRejected` to 502. The text of
every one of them is shown to the administrator, so none may carry a token.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class IngestError(Exception):
    """Base of the failures that come from the ingester or its catalog."""


class IngestUnavailable(IngestError):  # noqa: N818 - a state, not a failure of one request
    """The ingester cannot be used: not reachable, no token, or the token is refused."""


class IngestRejected(IngestError):  # noqa: N818
    """The ingester answered a request with an error status."""

    def __init__(self, status: int, detail: str, body: Any = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.body = body


class IngestTooOld(IngestError):  # noqa: N818
    """The ingester does not know the run option "add and update, never delete"."""


class CatalogRejected(IngestError):  # noqa: N818
    """The ingester did not take the manager's change to `jobs.yaml`, or it cannot be written."""

    def __init__(self, message: str, problems: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.problems = problems


class CatalogConflict(CatalogRejected):  # noqa: N818
    """The change no longer applies: the job exists already, is gone, or was edited meanwhile."""


class InvalidRequest(ValueError):  # noqa: N818 - reads as the answer it becomes: a 422
    """The request names something that cannot be embedded."""


class InvalidJob(InvalidRequest):  # noqa: N818
    """A job or catalog that would not be valid, with every problem found and the field of each."""

    def __init__(self, issues: Sequence[Any]) -> None:
        self.issues = tuple(issues)
        first = str(self.issues[0]) if self.issues else "The job is not valid."
        extra = f" (+{len(self.issues) - 1} more)" if len(self.issues) > 1 else ""
        super().__init__(first + extra)


class TooLarge(InvalidRequest):  # noqa: N818 - answered with a 413
    """A file or an upload is over the configured size."""


class Conflict(Exception):  # noqa: N818
    """The request clashes with what is running or what is on disk."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


class NotFound(Exception):  # noqa: N818
    """An upload, a run or a collection does not exist."""
