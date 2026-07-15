"""Health check endpoint — unauthenticated, for Docker and load-balancers."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
