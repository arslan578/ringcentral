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
os.environ.setdefault("RC_CLIENT_ID", "test-client-id")
os.environ.setdefault("RC_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("RC_JWT_TOKEN", "test-jwt-token")
os.environ.setdefault("RC_SERVER_URL", "https://platform.ringcentral.com")

# ── Now safe to import app modules ────────────────────────────────
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services.rc_api_client import RCApiClient


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


def make_mock_rc_api_client(messages: list[dict] | None = None) -> RCApiClient:
    """
    Returns a mock RCApiClient whose get_messages_batch() returns the given
    list of full RC message dicts.
    """
    mock = MagicMock(spec=RCApiClient)
    mock.get_messages_batch = AsyncMock(return_value=messages or [])
    mock.get_message = AsyncMock(return_value=messages[0] if messages else None)
    return mock


# ─────────────────────────────────────────────────────────────────
# REAL RC Notification Payload (what RC webhook actually sends)
# This is a CHANGE NOTIFICATION — NOT the actual message content.
# It only contains message IDs, not from/to/body.
# ─────────────────────────────────────────────────────────────────
RC_NOTIFICATION_PAYLOAD = {
    "uuid": "47954648896411110707",
    "event": "/restapi/v1.0/account/315079026/extension/2582602027/message-store",
    "timestamp": "2026-02-26T21:14:57.833Z",
    "subscriptionId": "7fd32175-9666-4dbc-9c7b-2af4c4f66cab",
    "ownerId": "258260202",
    "body": {
        "accountId": 315079026,
        "extensionId": 2582602027,
        "lastUpdated": "2026-02-26T21:14:43.560Z",
        "changes": [
            {
                "type": "SMS",
                "newCount": 2,
                "updatedCount": 0,
                "newMessageIds": [3610703867026, 3610703807026],
            }
        ],
    },
}

# ─────────────────────────────────────────────────────────────────
# Full RC Message objects (what the RC REST API returns)
# These are fetched by the app using the message IDs above.
# ─────────────────────────────────────────────────────────────────
RC_FULL_INBOUND_MESSAGE = {
    "id": 3610703867026,
    "uri": "https://platform.ringcentral.com/restapi/v1.0/account/315079026/extension/2582602027/message-store/3610703867026",
    "type": "SMS",
    "direction": "Inbound",
    "from": {"phoneNumber": "+15550001111", "name": "John Doe", "location": "Irvine, CA"},
    "to": [{"phoneNumber": "+15559990000", "name": "RC User", "location": "New York, NY"}],
    "subject": "Please remove me from your list",
    "creationTime": "2026-02-26T21:14:40.000Z",
    "lastModifiedTime": "2026-02-26T21:14:41.000Z",
    "readStatus": "Unread",
    "priority": "Normal",
    "availability": "Alive",
    "messageStatus": "Received",
    "conversationId": "conv-abc123",
    "conversation": {"id": "conv-abc123", "uri": "https://platform.ringcentral.com/restapi/v1.0/conversation/conv-abc123"},
    "smsDeliveryTime": "2026-02-26T21:14:40.000Z",
}

RC_FULL_OUTBOUND_MESSAGE = {
    "id": 3610703807026,
    "uri": "https://platform.ringcentral.com/restapi/v1.0/account/315079026/extension/2582602027/message-store/3610703807026",
    "type": "SMS",
    "direction": "Outbound",
    "from": {"phoneNumber": "+15559990000", "name": "RC User", "location": "New York, NY"},
    "to": [{"phoneNumber": "+15550001111", "name": "John Doe", "location": "Irvine, CA"}],
    "subject": "Hi there, how can we help you?",
    "creationTime": "2026-02-26T21:13:30.000Z",
    "lastModifiedTime": "2026-02-26T21:13:31.000Z",
    "readStatus": "Read",
    "priority": "Normal",
    "availability": "Alive",
    "messageStatus": "Sent",
    "conversationId": "conv-abc123",
    "conversation": {"id": "conv-abc123", "uri": "https://platform.ringcentral.com/restapi/v1.0/conversation/conv-abc123"},
    "smsDeliveryTime": "2026-02-26T21:13:30.000Z",
}

# Keep old name for backward compatibility with unchanged tests
RC_INBOUND_SMS_PAYLOAD = RC_NOTIFICATION_PAYLOAD
