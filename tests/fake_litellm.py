"""A LiteLLM that speaks just enough of its HTTP API, for the Models page tests.

An in-memory fake behind `httpx.MockTransport`, in the shapes LiteLLM 1.100 answers with: the
day entries of `/user/daily/activity` (with `breakdown.model_groups`, a `has_more` flag and the
rows-not-days paging), the deployments of `/model/info` and the unauthenticated
`/health/readiness`. It reproduces what the manager depends on:

* the key-protected routes answer 401 to any bearer that is not `MASTER_KEY`;
* a model without a price in `model_info` has none of the cost fields, and its spend is 0 while
  its tokens are still counted;
* with `split_days` a day arrives as one entry per model, so that a page break can fall inside
  a day and the caller has to add the pieces up.

`requests` records every request as `(method, path)`, so a test can check that the manager sent
nothing but reads to the routes it is allowed to use. `down` makes every call fail at the
transport, `fail` makes the usage route answer with a 500, `garbage` answers with something that
is not JSON.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx

MASTER_KEY = "sk-test-master-key"

# One row per deployment, as `/model/info` lists them. Prices are per token.
DEPLOYMENTS: list[dict[str, Any]] = [
    {
        "model_name": "gpt-4o-mini",
        "litellm_params": {"model": "openai/gpt-4o-mini"},
        "model_info": {
            "mode": "chat",
            "input_cost_per_token": 0.00000015,
            "output_cost_per_token": 0.0000006,
        },
    },
    {
        "model_name": "chat-large",
        "litellm_params": {"model": "vendor/chat-large"},
        "model_info": {
            "mode": "chat",
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
        },
    },
    {
        "model_name": "qwen-local",
        "litellm_params": {"model": "openai/qwen", "api_base": "http://localai:8080/v1"},
        "model_info": {"mode": "chat"},
    },
    {
        "model_name": "bge-m3",
        "litellm_params": {"model": "openai/bge-m3", "api_base": "http://localai:8080/v1"},
        "model_info": {"mode": "embedding"},
    },
    {
        "model_name": "never-used",
        "litellm_params": {"model": "openai/never-used"},
        "model_info": {
            "mode": "chat",
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000002,
        },
    },
]


def usage_row(
    prompt: int = 0, completion: int = 0, spend: float = 0.0, ok: int = 0, failed: int = 0
) -> dict[str, Any]:
    """The `metrics` of one model group on one day."""
    return {
        "spend": spend,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "successful_requests": ok,
        "failed_requests": failed,
        "api_requests": ok + failed,
    }


class FakeLitellm:
    def __init__(self) -> None:
        self.deployments: list[dict[str, Any]] = [dict(item) for item in DEPLOYMENTS]
        # day -> model group -> metrics
        self.usage: dict[date, dict[str, dict[str, Any]]] = {}
        self.requests: list[tuple[str, str]] = []
        self.params: list[dict[str, str]] = []
        self.down = False
        self.fail = False
        self.garbage = False
        self.master_key = MASTER_KEY
        self.split_days = False
        self.database = "connected"

    # ── what a test sets up ─────────────────────────────────────────────────

    def use(self, day: date, model: str, **metrics: Any) -> None:
        """Record that `model` consumed something on `day` (see `usage_row` for the fields)."""
        self.usage.setdefault(day, {})[model] = usage_row(**metrics)

    def calls(self, path: str) -> int:
        return sum(1 for _, seen in self.requests if seen == path)

    @property
    def methods(self) -> set[str]:
        return {method for method, _ in self.requests}

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ── the server ──────────────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        self.params.append(dict(request.url.params))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.garbage:
            return httpx.Response(200, content=b"<html>not json</html>")
        if path == "/health/readiness":
            return httpx.Response(200, json={"status": "healthy", "db": self.database})
        if request.headers.get("authorization") != f"Bearer {self.master_key}":
            return httpx.Response(
                401, json={"error": {"message": "Authentication Error", "type": "auth_error"}}
            )
        if path == "/model/info":
            return httpx.Response(200, json={"data": self.deployments})
        if path == "/user/daily/activity":
            if self.fail:
                return httpx.Response(500, json={"error": {"message": "database is gone"}})
            return self._daily(request)
        return httpx.Response(404, json={"detail": "Not Found"})

    def _entries(self, start: date, end: date) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        day = start
        while day <= end:
            groups = self.usage.get(day)
            if groups:
                pieces = [{name: item} for name, item in groups.items()]
                for piece in pieces if self.split_days else [groups]:
                    entries.append(self._entry(day, piece))
            day += timedelta(days=1)
        return entries

    @staticmethod
    def _entry(day: date, groups: dict[str, dict[str, Any]]) -> dict[str, Any]:
        total = usage_row()
        for item in groups.values():
            for field, value in item.items():
                total[field] += value
        return {
            "date": day.isoformat(),
            "metrics": total,
            "breakdown": {
                "models": {f"openai/{name}": {"metrics": item} for name, item in groups.items()},
                "model_groups": {name: {"metrics": item} for name, item in groups.items()},
            },
        }

    def _daily(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params
        start = date.fromisoformat(query["start_date"])
        end = date.fromisoformat(query["end_date"])
        page = int(query.get("page", "1"))
        size = int(query.get("page_size", "50"))
        entries = self._entries(start, end)
        chunk = entries[(page - 1) * size : page * size]
        return httpx.Response(
            200,
            json={
                "results": chunk,
                "metadata": {
                    "total_spend": 0.0,
                    "page": page,
                    "total_pages": max(1, -(-len(entries) // size)),
                    "has_more": page * size < len(entries),
                },
            },
        )
