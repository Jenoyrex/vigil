from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.api.v1.analytics import router as analytics_router
from app.api.v1.evaluations import router as evaluations_router
from app.api.v1.traces import router as traces_router
from app.clickhouse.client import get_clickhouse_client
from app.config import settings
from app.db.session import ping_database
from app.logging_config import configure_logging
from app.middleware import MaxBodySizeMiddleware, RequestIdMiddleware

# Structured (JSON Lines) logging (Phase 4D, F4) -- see
# app/logging_config.py's module docstring. Replaces the previous
# logging.basicConfig(level=logging.INFO) plain-text setup.
configure_logging(service="api", level=settings.log_level)

app = FastAPI(title=settings.app_name)
app.add_middleware(MaxBodySizeMiddleware, max_body_bytes=settings.max_request_body_bytes)
# Explicit deny-by-default CORS (Phase 4C) -- see app/config.py's
# `cors_allowed_origins`/`cors_allowed_origins_list` and docs/decisions/
# 007-cors-and-dashboard-security-headers.md. `allow_credentials=False`
# deliberately: this API authenticates via `Authorization: Bearer <key>`,
# never cookies, so credentialed CORS has no purpose here and combining it
# with a configured origin list would only add risk for zero benefit.
# `allow_methods`/`allow_headers` are scoped to exactly what this API's
# routes actually use, not `["*"]`. Added after MaxBodySizeMiddleware (and,
# as of Phase 4D, before RequestIdMiddleware -- see that middleware's own
# docstring for why it is deliberately the outermost layer now) so a
# cross-origin preflight (OPTIONS) request is still answered before it
# would otherwise reach body-size checks or routing.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Authorization", "Content-Type"],
)
# Registered last (see app/middleware.py's RequestIdMiddleware docstring) so
# it becomes the OUTERMOST middleware -- every request gets a request_id
# bound for structured-log correlation, and an X-Request-Id response
# header, before CORS/body-size handling or routing ever runs.
app.add_middleware(RequestIdMiddleware)
app.include_router(traces_router)
app.include_router(analytics_router)
app.include_router(evaluations_router)


class HealthResponse(BaseModel):
    status: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


class ReadyResponse(BaseModel):
    status: str
    clickhouse: str
    postgresql: str


@app.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"description": "A backing store is unreachable."}},
    summary="Readiness check",
    description=(
        "Unlike `/health`, this checks both ClickHouse and PostgreSQL "
        "connectivity and can return 503 if either is unreachable. Kept "
        "separate so `/health` stays a pure liveness check that never "
        "depends on a backing store being reachable. ClickHouse is checked "
        "first, then PostgreSQL -- either failing short-circuits to a 503 "
        "with a store-specific detail message, never a raw driver "
        "exception."
    ),
)
def ready() -> ReadyResponse:
    try:
        get_clickhouse_client().ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="ClickHouse is unreachable.") from exc
    try:
        ping_database()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL is unreachable.") from exc
    return ReadyResponse(status="ok", clickhouse="ok", postgresql="ok")
