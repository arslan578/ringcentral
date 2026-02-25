"""
app/api/v1/router.py

Aggregates all v1 endpoint routers into a single APIRouter
that is mounted at /api/v1 in main.py.
"""
from fastapi import APIRouter

from app.api.v1.endpoints import health, rc_webhook

api_v1_router = APIRouter()

# Mount sub-routers
api_v1_router.include_router(health.router)
api_v1_router.include_router(rc_webhook.router)
