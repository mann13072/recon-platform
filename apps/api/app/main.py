"""The FastAPI application.

Base path is ``/api/v1`` (spec section 37). Everything under it requires a
verified bearer token except the health endpoints.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from apps.api.app.api import (
    audit,
    exceptions,
    files,
    matches,
    me,
    reconciliations,
    transactions,
)
from apps.api.app.config import get_settings
from packages.ai.privacy import AIDisabledError, DataRegionViolation
from packages.controls.period_lock import PeriodLocked
from packages.controls.permissions import PermissionDenied
from packages.controls.segregation_of_duties import SoDViolation
from packages.exceptions.workflow import IllegalTransition
from packages.matching.engine import MatchExclusivityError

logger = logging.getLogger("recon.api")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    logger.info(
        "starting recon-platform api",
        extra={"environment": settings.environment, "ai_enabled": settings.ai_enabled},
    )
    yield
    logger.info("stopping recon-platform api")


app = FastAPI(
    title="Reconciliation Platform API",
    version="0.1.0",
    description=(
        "Deterministic accounting logic first. AI assists ambiguity; it does not "
        "own financial truth."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_and_security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Attach a correlation ID and set the baseline security headers.

    Every job and audit event carries the correlation ID, so one user action can
    be traced end to end (spec section 62).
    """
    correlation = request.headers.get("X-Correlation-ID")
    try:
        request.state.correlation_id = uuid.UUID(correlation) if correlation else uuid.uuid4()
    except ValueError:
        request.state.correlation_id = uuid.uuid4()

    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000

    response.headers["X-Correlation-ID"] = str(request.state.correlation_id)
    response.headers["X-Response-Time-ms"] = f"{duration_ms:.1f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _problem(
    request: Request, status_code: int, title: str, detail: str, code: str = ""
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "about:blank",
            "title": title,
            "status": status_code,
            "detail": detail,
            "code": code,
            "instance": str(request.url.path),
        },
    )


@app.exception_handler(PermissionDenied)
async def _permission_denied(request: Request, exc: PermissionDenied) -> JSONResponse:
    return _problem(request, status.HTTP_403_FORBIDDEN, "Forbidden", str(exc), "permission_denied")


@app.exception_handler(SoDViolation)
async def _sod_violation(request: Request, exc: SoDViolation) -> JSONResponse:
    return _problem(request, status.HTTP_409_CONFLICT, "Control violation", str(exc), exc.code)


@app.exception_handler(PeriodLocked)
async def _period_locked(request: Request, exc: PeriodLocked) -> JSONResponse:
    return _problem(request, status.HTTP_409_CONFLICT, "Period locked", str(exc), exc.code)


@app.exception_handler(IllegalTransition)
async def _illegal_transition(request: Request, exc: IllegalTransition) -> JSONResponse:
    return _problem(request, status.HTTP_409_CONFLICT, "Illegal transition", str(exc), exc.code)


@app.exception_handler(MatchExclusivityError)
async def _exclusivity(request: Request, exc: MatchExclusivityError) -> JSONResponse:
    # An invariant violation is a bug, not a user error. Say so plainly.
    logger.error("match exclusivity violated: %s", exc)
    return _problem(
        request,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "Accounting integrity check failed",
        "The operation was refused because it would have violated match "
        "exclusivity. Nothing was saved.",
        "invariant_violation",
    )


@app.exception_handler(AIDisabledError)
async def _ai_disabled(request: Request, exc: AIDisabledError) -> JSONResponse:
    return _problem(request, status.HTTP_409_CONFLICT, "AI unavailable", str(exc), "ai_disabled")


@app.exception_handler(DataRegionViolation)
async def _ai_region(request: Request, exc: DataRegionViolation) -> JSONResponse:
    return _problem(request, status.HTTP_409_CONFLICT, "AI unavailable", str(exc), "data_region")


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "type": "about:blank",
            "title": "Invalid request",
            "status": 422,
            "detail": "The request body did not validate.",
            "code": "validation_error",
            "errors": exc.errors(),
            "instance": str(request.url.path),
        },
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def readiness() -> dict[str, str]:
    """Readiness includes the database, because an API that cannot reach its
    database should not receive traffic."""
    from sqlalchemy import text

    from apps.api.app.infrastructure.db import get_engine

    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - depends on deployment
        return {"status": "degraded", "database": f"unavailable: {type(exc).__name__}"}
    return {"status": "ok", "database": "ok"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

app.include_router(me.router, prefix=API_PREFIX)
app.include_router(files.router, prefix=API_PREFIX)
app.include_router(transactions.router, prefix=API_PREFIX)
app.include_router(reconciliations.router, prefix=API_PREFIX)
app.include_router(matches.router, prefix=API_PREFIX)
app.include_router(exceptions.router, prefix=API_PREFIX)
app.include_router(audit.router, prefix=API_PREFIX)
