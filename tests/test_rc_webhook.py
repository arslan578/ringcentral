"""
tests/test_rc_webhook.py

Integration-level tests for the RC webhook endpoint.
Uses FastAPI TestClient with a mocked httpx client injected into app state.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.core.idempotency import IdempotencyCache
from app.services.zapier_forwarder import ForwardResult, ZapierForwarder
from tests.conftest import RC_INBOUND_SMS_PAYLOAD, VALID_TOKEN


def _make_mock_forwarder(status_code: int = 200, raises=None) -> ZapierForwarder:
    """Helper: returns a ZapierForwarder whose send() is mocked."""
    forwarder = MagicMock(spec=ZapierForwarder)
    if raises:
        forwarder.send = AsyncMock(side_effect=raises)
    else:
        forwarder.send = AsyncMock(
            return_value=ForwardResult(
                success=True,
                attempts=1,
                final_status_code=status_code,
                message_id="msg-001",
            )
        )
    return forwarder


# ─────────────────────────────────────────────────────────────────
# Validation Challenge (GET)
# ─────────────────────────────────────────────────────────────────

def test_validation_challenge_returns_token(client: TestClient):
    response = client.get("/api/v1/rc/webhook?validationToken=my-secret-token")
    assert response.status_code == 200
    assert response.text == "my-secret-token"


def test_validation_challenge_missing_token(client: TestClient):
    response = client.get("/api/v1/rc/webhook")
    assert response.status_code == 400


# ─────────────────────────────────────────────────────────────────
# POST — Authentication
# ─────────────────────────────────────────────────────────────────

def test_post_missing_verification_token(client: TestClient):
    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_INBOUND_SMS_PAYLOAD,
        # No Verification-Token header
    )
    assert response.status_code == 401


def test_post_wrong_verification_token(client: TestClient):
    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_INBOUND_SMS_PAYLOAD,
        headers={"Verification-Token": "wrong-token"},
    )
    assert response.status_code == 401


# ─────────────────────────────────────────────────────────────────
# POST — Happy path
# ─────────────────────────────────────────────────────────────────

def test_inbound_sms_forwarded_successfully(app, client: TestClient):
    # Override app state with mock forwarder
    app.state.zapier_forwarder = _make_mock_forwarder(status_code=200)
    app.state.idempotency_cache = IdempotencyCache(maxsize=100, ttl=60)

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_INBOUND_SMS_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "forwarded"
    assert body["message_id"] == "msg-001"
    assert body["zapier_status_code"] == 200


# ─────────────────────────────────────────────────────────────────
# POST — Filtering
# ─────────────────────────────────────────────────────────────────

def test_outbound_message_ignored(app, client: TestClient):
    app.state.zapier_forwarder = _make_mock_forwarder()
    app.state.idempotency_cache = IdempotencyCache(maxsize=100, ttl=60)

    payload = dict(RC_INBOUND_SMS_PAYLOAD)
    payload["body"] = dict(payload["body"])
    payload["body"]["direction"] = "Outbound"

    response = client.post(
        "/api/v1/rc/webhook",
        json=payload,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert "outbound" in response.json()["reason"]


def test_non_sms_event_ignored(app, client: TestClient):
    app.state.zapier_forwarder = _make_mock_forwarder()
    app.state.idempotency_cache = IdempotencyCache(maxsize=100, ttl=60)

    payload = dict(RC_INBOUND_SMS_PAYLOAD)
    payload["body"] = dict(payload["body"])
    payload["body"]["type"] = "Voicemail"

    response = client.post(
        "/api/v1/rc/webhook",
        json=payload,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


# ─────────────────────────────────────────────────────────────────
# POST — Idempotency
# ─────────────────────────────────────────────────────────────────

def test_duplicate_message_suppressed(app, client: TestClient):
    mock_forwarder = _make_mock_forwarder()
    cache = IdempotencyCache(maxsize=100, ttl=60)
    app.state.zapier_forwarder = mock_forwarder
    app.state.idempotency_cache = cache

    # First POST — should forward
    r1 = client.post(
        "/api/v1/rc/webhook",
        json=RC_INBOUND_SMS_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert r1.json()["status"] == "forwarded"

    # Second POST with same messageId — should be suppressed
    r2 = client.post(
        "/api/v1/rc/webhook",
        json=RC_INBOUND_SMS_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert r2.json()["status"] == "duplicate"

    # Forwarder was called exactly once
    assert mock_forwarder.send.call_count == 1
