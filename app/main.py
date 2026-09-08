"""FastAPI application factory, middleware and error handling."""
from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.database import Base, engine
from app.routers import advisory, auth, portfolio, transactions, users

def _configure_logging() -> None:
    """Send application logs to stdout at a level the operator chooses.

    Without this the "app" logger has no handler and the root level is WARNING,
    so every logger.info below is silently discarded - request ids would appear
    in response headers with nothing in the logs to match them to. uvicorn
    configures only its own loggers, not ours.
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if not app_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        app_logger.addHandler(handler)
    # Already emitted by our own handler; let uvicorn's root config alone.
    app_logger.propagate = False


_configure_logging()
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.auto_create_tables:
        # Development convenience. In production this is off by default and the
        # schema is created by `python -m scripts.init_db` as a release step -
        # running create_all from N workers at once is a race.
        Base.metadata.create_all(bind=engine)
    else:
        logger.info("auto_create_tables disabled; assuming schema already exists")

    # Fail fast on a bad DATABASE_URL rather than surfacing it as a 500 on the
    # first real request.
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    logger.info("database reachable, environment=%s", settings.environment)
    yield


app = FastAPI(
    title="AI Finance QA Platform",
    version="1.0.0",
    description=(
        "A small investing API with an LLM-backed explanation layer, built as "
        "the system under test for an automated QA suite."
    ),
    lifespan=lifespan,
    # The OpenAPI schema documents every bound and field; gated in production.
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id, time the request, and set security headers.

    The request id goes out on the response so a user-reported failure can be
    matched to a log line without guessing at timestamps.
    """
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    started = time.perf_counter()

    response = await call_next(request)

    duration_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    # BUG-002: a JSON body holding a user-supplied string is only inert while
    # the browser respects the content type. nosniff stops it being re-read as
    # HTML; DENY stops the API being framed.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"

    logger.info(
        "%s %s -> %s in %.1fms [%s]",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        request_id,
    )
    return response


@app.exception_handler(RequestValidationError)
def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return 422 with the field-level detail intact.

    The test suite asserts on which field failed, not just the status code, so
    the per-field errors must survive into the body.
    """
    # BUG-001: exc.errors() carries the constraint context, and for a Decimal
    # bound that ctx holds a Decimal, which json.dumps cannot encode. Returning
    # it raw made the handler raise, turning every out-of-range money value
    # into a 500 instead of a 422. jsonable_encoder flattens it first.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": jsonable_encoder(exc.errors())},
    )


@app.exception_handler(Exception)
def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log the cause, return nothing about it.

    Starlette already returns a bare 'Internal Server Error' body, but by
    default the traceback only reaches stderr unformatted. This records it
    against the request id and keeps the response opaque.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    """Liveness only - deliberately does not touch the database.

    A load balancer polling this must not open a connection per probe.
    """
    return {"status": "ok"}


@app.get("/health/ready", tags=["ops"])
def readiness() -> JSONResponse:
    """Readiness - checks the dependency that can actually be down."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readiness probe failed")
        return JSONResponse(
            status_code=503, content={"status": "unavailable", "database": "down"}
        )
    return JSONResponse(status_code=200, content={"status": "ready", "database": "up"})


app.include_router(users.router)
app.include_router(auth.router)
app.include_router(portfolio.router)
app.include_router(transactions.router)
app.include_router(advisory.router)
