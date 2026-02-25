"""
app/api/v1/endpoints/health.py

Health check endpoint — used by Docker HEALTHCHECK, load balancers,
and uptime monitors to verify the service is alive.
"""
from fastapi import APIRouter, Request

from app.config import get_settings

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    summary="Health Check",
    description="Returns service liveness status. Used by Docker HEALTHCHECK and monitors.",
)
async def health_check(request: Request) -> dict:
    settings = get_settings()

    # Check idempotency cache is accessible
    cache_size = getattr(request.app.state, "idempotency_cache", None)
    cache_status = "ok" if cache_size is not None else "unavailable"

    return {
        "status": "ok",
        "service": "rc-sms-webhook",
        "version": "1.0.0",
        "environment": settings.app_env,
        "idempotency_cache": cache_status,
        "cache_size": (
            request.app.state.idempotency_cache.size
            if cache_status == "ok"
            else None
        ),
    }
