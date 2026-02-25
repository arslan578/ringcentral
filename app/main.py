"""
app/main.py

FastAPI application factory.

Uses the `lifespan` context manager (FastAPI 0.93+) to:
  - Start structured logging
  - Create and share a single httpx.AsyncClient (connection pool)
  - Initialize the ZapierForwarder service
  - Initialize the IdempotencyCache
  - Tear everything down cleanly on shutdown

All shared resources are stored on `app.state` for injection into endpoints.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_v1_router
from app.config import get_settings
from app.core.idempotency import IdempotencyCache
from app.core.logging import setup_logging
from app.services.zapier_forwarder import ZapierForwarder

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs on startup (before serving requests) and teardown (after shutdown).
    FastAPI's recommended replacement for @app.on_event("startup").
    """
    settings = get_settings()

    # ── Startup ─────────────────────────────────────────────────────
    setup_logging(settings.log_level)
    logger.info(
        "RC SMS Webhook service starting",
        extra={"env": settings.app_env, "log_level": settings.log_level},
    )

    # Shared async HTTP client — reused across all requests
    http_client = httpx.AsyncClient(
        headers={
            "Content-Type": "application/json",
            "User-Agent": "rc-sms-webhook/1.0.0",
        },
        timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0),
    )

    # Attach shared resources to app state
    app.state.http_client = http_client
    app.state.zapier_forwarder = ZapierForwarder(
        webhook_url=settings.zapier_webhook_url,
        http_client=http_client,
        max_retries=settings.zapier_max_retries,
        base_delay=settings.zapier_retry_base_delay_seconds,
    )
    app.state.idempotency_cache = IdempotencyCache(
        maxsize=settings.idempotency_cache_max_size,
        ttl=settings.idempotency_cache_ttl_seconds,
    )

    logger.info(
        "Startup complete — ready to receive RC webhooks",
        extra={
            "zapier_url": settings.zapier_webhook_url,
            "max_retries": settings.zapier_max_retries,
        },
    )

    yield  # ← Service is live here

    # ── Shutdown ────────────────────────────────────────────────────
    logger.info("RC SMS Webhook service shutting down")
    await http_client.aclose()
    logger.info("HTTP client closed. Shutdown complete.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title="RC Inbound SMS → Zapier Webhook Integration",
        description=(
            "Routes 100% of inbound RingCentral SMS messages to a Zapier webhook "
            "in near real-time for automated DNC language detection. "
            "Includes retry logic, deduplication, and structured logging."
        ),
        version="1.0.0",
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
        lifespan=lifespan,
    )

    # ── Middleware ──────────────────────────────────────────────────
    # CORS: RC only calls from their servers, but keep it explicit.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ── Global exception handlers ───────────────────────────────────
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.error(
            "Unhandled exception",
            extra={
                "path": str(request.url),
                "method": request.method,
                "error": str(exc),
            },
            exc_info=exc,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    # ── Routers ─────────────────────────────────────────────────────
    app.include_router(api_v1_router, prefix="/api/v1")

    return app


# Application entry point
app = create_app()
