import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.api.v1.analytics import router as analytics_router
from app.api.v1.traces import router as traces_router
from app.clickhouse.client import get_clickhouse_client
from app.config import settings
from app.middleware import MaxBodySizeMiddleware

logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name)
app.add_middleware(MaxBodySizeMiddleware, max_body_bytes=settings.max_request_body_bytes)
app.include_router(traces_router)
app.include_router(analytics_router)


class HealthResponse(BaseModel):
    status: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


class ReadyResponse(BaseModel):
    status: str
    clickhouse: str


@app.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"description": "A backing store is unreachable."}},
    summary="Readiness check",
    description=(
        "Unlike `/health`, this checks ClickHouse connectivity and can "
        "return 503. Kept separate so `/health` stays a pure liveness check "
        "that never depends on a backing store being reachable."
    ),
)
def ready() -> ReadyResponse:
    try:
        get_clickhouse_client().ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="ClickHouse is unreachable.") from exc
    return ReadyResponse(status="ok", clickhouse="ok")
