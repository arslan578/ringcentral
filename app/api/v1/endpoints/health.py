"""
app/api/v1/endpoints/health.py

Health check endpoint — used by Docker HEALTHCHECK, load balancers,
and uptime monitors to verify the service is alive.

Also shows RC webhook subscription status so you can verify
the subscription is active without checking the RC Developer Console.
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
    cache_obj = getattr(request.app.state, "idempotency_cache", None)
    cache_status = "ok" if cache_obj is not None else "unavailable"

    # Check subscription manager status
    sub_manager = getattr(request.app.state, "subscription_manager", None)
    subscription_info = (
        sub_manager.status.to_dict() if sub_manager else {"status": "auto_disabled"}
    )

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
        "rc_subscription": subscription_info,
    }
