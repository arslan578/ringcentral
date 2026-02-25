"""
tests/conftest.py

pytest fixtures shared across all test modules.

IMPORTANT: env vars must be set BEFORE any app module is imported,
because pydantic-settings resolves them at class definition time.
"""
from __future__ import annotations

# ── Set test env vars FIRST — before any app import ──────────────
import os

VALID_TOKEN = "test-verification-token-abc123"

os.environ.setdefault("RC_WEBHOOK_VERIFICATION_TOKEN", VALID_TOKEN)
os.environ.setdefault("ZAPIER_WEBHOOK_URL", "https://hooks.zapier.com/hooks/catch/test/")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("LOG_LEVEL", "DEBUG")

# ── Now safe to import app modules ────────────────────────────────
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


@pytest.fixture
def app():
    """Create a fresh FastAPI test application."""
    get_settings.cache_clear()
    return create_app()


@pytest.fixture
def client(app):
    """Synchronous TestClient for endpoint tests."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# Sample RC inbound SMS payload matching real RC schema
RC_INBOUND_SMS_PAYLOAD = {
    "uuid": "test-uuid-001",
    "event": "/restapi/v1.0/account/~/extension/~/message-store",
    "timestamp": "2026-02-25T01:00:00Z",
    "subscriptionId": "sub-001",
    "ownerId": "account-999",
    "body": {
        "id": "msg-001",
        "uri": "https://platform.ringcentral.com/restapi/v1.0/account/~/extension/~/message-store/msg-001",
        "type": "SMS",
        "direction": "Inbound",
        "from": {"phoneNumber": "+15550001111", "name": "John Doe"},
        "to": [{"phoneNumber": "+15559990000", "name": "RC User"}],
        "subject": "Please remove me from your list",
        "creationTime": "2026-02-25T00:59:50Z",
        "lastModifiedTime": "2026-02-25T00:59:51Z",
        "readStatus": "Unread",
        "priority": "Normal",
        "availability": "Alive",
        "messageStatus": "Received",
        "conversationId": "conv-abc123",
    },
}
