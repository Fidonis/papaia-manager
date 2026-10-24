"""REST API: the models LiteLLM serves and what they consumed. Read-only, administrators only.

The same reading the Models page draws, as JSON. A window the API cannot make sense of is a 422;
LiteLLM that cannot be read (down, no key, a key it refuses, an answer that makes no sense) is a
503 that says which.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.deps import AdminUser
from app.config import Settings, get_settings
from app.core.model_usage import (
    DEFAULT_RANGE,
    InvalidWindow,
    Metrics,
    ModelEntry,
    Reading,
    UsageReport,
    load_reading,
    resolve_window,
)
from app.routers.models_deps import LitellmTransport

router = APIRouter(prefix="/api/v1/models")


class ModelOut(BaseModel):
    name: str
    provider: str
    host: str
    mode: str
    input_per_million: float | None
    output_per_million: float | None
    priced: bool
    deployments: int


class CatalogOut(BaseModel):
    models: list[ModelOut]


class MetricsOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    spend: float
    requests: int
    failed_requests: int


class DayOut(MetricsOut):
    date: str


class UsageModelOut(BaseModel):
    name: str
    configured: bool
    totals: MetricsOut
    daily: list[MetricsOut]


class UsageOut(BaseModel):
    range: str
    start: str
    end: str
    days: list[str]
    complete: bool
    totals: MetricsOut
    daily: list[DayOut]
    models: list[UsageModelOut]


def _metrics(metrics: Metrics) -> dict[str, Any]:
    return {
        "prompt_tokens": metrics.prompt_tokens,
        "completion_tokens": metrics.completion_tokens,
        "total_tokens": metrics.tokens,
        "spend": metrics.spend,
        "requests": metrics.requests,
        "failed_requests": metrics.failed,
    }


def _model(entry: ModelEntry) -> ModelOut:
    return ModelOut(
        name=entry.name,
        provider=entry.provider,
        host=entry.host,
        mode=entry.mode,
        input_per_million=entry.input_per_million,
        output_per_million=entry.output_per_million,
        priced=entry.priced,
        deployments=entry.deployments,
    )


def _report(reading: Reading) -> UsageReport:
    if reading.report is None:
        raise HTTPException(
            status_code=503,
            detail=reading.problem.text() if reading.problem else "LiteLLM cannot be read",
        )
    return reading.report


@router.get("", response_model=CatalogOut)
async def list_models(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: LitellmTransport,
) -> CatalogOut:
    """The models LiteLLM serves, with their prices per million tokens."""
    reading = await load_reading(settings, resolve_window(DEFAULT_RANGE), transport=transport)
    report = _report(reading)
    return CatalogOut(models=[_model(m.entry) for m in report.models if m.entry is not None])


@router.get("/usage", response_model=UsageOut)
async def usage(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: LitellmTransport,
    range: Annotated[str, Query(max_length=16)] = DEFAULT_RANGE,  # noqa: A002
    from_: Annotated[str, Query(alias="from", max_length=16)] = "",
    to: Annotated[str, Query(max_length=16)] = "",
) -> UsageOut:
    """Tokens, spend and requests per day and per model for a window of UTC days.

    `range` is `7d` (the default: today and the six days before), `30d`, `90d`, `month`
    (the first of the month until today) or `custom` with `from` and `to` as `YYYY-MM-DD`.
    """
    try:
        window = resolve_window(range, from_, to)
    except InvalidWindow as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    report = _report(await load_reading(settings, window, transport=transport))
    return UsageOut(
        range=window.key,
        start=window.start.isoformat(),
        end=window.end.isoformat(),
        days=[day.isoformat() for day in report.days],
        complete=report.complete,
        totals=MetricsOut(**_metrics(report.totals)),
        daily=[
            DayOut(date=day.isoformat(), **_metrics(metrics))
            for day, metrics in zip(report.days, report.daily_totals, strict=True)
        ],
        models=[
            UsageModelOut(
                name=model.name,
                configured=model.configured,
                totals=MetricsOut(**_metrics(model.totals)),
                daily=[MetricsOut(**_metrics(metrics)) for metrics in model.daily],
            )
            for model in report.models
        ],
    )
