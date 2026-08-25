"""FastAPI application entry point for the AI CTO Hub orchestrator.

Phase 1 scope: application lifecycle, structured logging, configuration,
database initialization, error handling foundation, and a liveness endpoint.
No task or agent functionality is implemented yet.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import settings
from .db import dispose_engine, init_db
from .logging_config import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Application startup and shutdown handling."""
    setup_logging(settings.log_level)
    logger.info(
        "application starting",
        extra={
            "app": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
        },
    )
    try:
        init_db()
    except Exception:
        logger.exception(
            "database initialization failed",
            extra={"database_url": settings.database_url},
        )
        raise
    logger.info("application started", extra={"database_url": settings.database_url})
    yield
    dispose_engine()
    logger.info("application stopped")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Backend foundation for orchestrating coding agents (Phase 1).",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Error handling foundation: log unexpected errors and return JSON 500."""
    logger.exception(
        "unhandled exception",
        extra={"path": str(request.url.path), "method": request.method},
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Liveness probe. Returns 200 with {"status": "ok"} when the process is up."""
    return {"status": "ok"}
