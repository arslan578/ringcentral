"""
tests/test_rc_webhook.py

Integration-level tests for the RC webhook endpoint.

Verifies the complete pipeline:
  1. RC sends a change notification (with message IDs, NOT content)
  2. App fetches full message data from RC API (mocked)
  3. App builds ZapierPayload with all required fields
  4. App forwards BOTH inbound and outbound SMS to Zapier

Tests verify against the SOW payload requirements:
  - Inbound phone number (sender)
  - Phone number of the RC user receiving the SMS
  - SMS message body/content
  - Message ID
  - Timestamp (UTC)
  - Account/user ID
  - Conversation/thread ID
  - Delivery status
  - Message direction flag
  - All available metadata
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.core.exceptions import ZapierForwardError
from app.core.idempotency import IdempotencyCache
from app.services.zapier_forwarder import ForwardResult, ZapierForwarder
from tests.conftest import (
    RC_NOTIFICATION_PAYLOAD,
    RC_FULL_INBOUND_MESSAGE,
    RC_FULL_OUTBOUND_MESSAGE,
    VALID_TOKEN,
    make_mock_rc_api_client,
)


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
                message_id="3610703867026",
            )
        )
    return forwarder


def _setup_app_state(app, messages=None, forwarder=None, cache=None):
    """Convenience: wire up mock forwarder, mock RC API client, and cache."""
    app.state.zapier_forwarder = forwarder or _make_mock_forwarder(status_code=200)
    app.state.idempotency_cache = cache or IdempotencyCache(maxsize=100, ttl=60)
    app.state.rc_api_client = make_mock_rc_api_client(
        messages=messages if messages is not None else [RC_FULL_INBOUND_MESSAGE]
    )


# ─────────────────────────────────────────────────────────────────
# Health Endpoint
# ─────────────────────────────────────────────────────────────────

def test_health_endpoint_returns_ok(client: TestClient):
    """GET /api/v1/health should return service status."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "rc-sms-webhook"
    assert body["version"] == "1.0.0"
    assert "environment" in body
    assert "idempotency_cache" in body


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
# POST — Validation-Token Challenge (subscription creation/renewal)
# ─────────────────────────────────────────────────────────────────

def test_post_validation_token_challenge(client: TestClient):
    """When RC sends a POST with a Validation-Token header during subscription
    creation, we must echo it back in the response header with 200 OK."""
    response = client.post(
        "/api/v1/rc/webhook",
        content=b"",
        headers={"Validation-Token": "rc-challenge-token-xyz"},
    )
    assert response.status_code == 200
    assert response.text == "rc-challenge-token-xyz"
    assert response.headers.get("Validation-Token") == "rc-challenge-token-xyz"


# ─────────────────────────────────────────────────────────────────
# POST — Authentication
# ─────────────────────────────────────────────────────────────────

def test_post_missing_verification_token(client: TestClient):
    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
    )
    assert response.status_code == 401


def test_post_wrong_verification_token(client: TestClient):
    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": "wrong-token"},
    )
    assert response.status_code == 401


# ─────────────────────────────────────────────────────────────────
# POST — Invalid / malformed body
# ─────────────────────────────────────────────────────────────────

def test_post_invalid_json_body(client: TestClient):
    """Sending non-JSON body should return 400."""
    response = client.post(
        "/api/v1/rc/webhook",
        content=b"this is not json",
        headers={
            "Verification-Token": VALID_TOKEN,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400
    assert "Invalid JSON" in response.json()["detail"]


# ─────────────────────────────────────────────────────────────────
# POST — Happy Path: Inbound SMS forwarded with all required fields
# ─────────────────────────────────────────────────────────────────

def test_inbound_sms_forwarded_successfully(app, client: TestClient):
    """Full pipeline: notification → fetch from RC API → forward to Zapier."""
    _setup_app_state(app, messages=[RC_FULL_INBOUND_MESSAGE])

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processed"
    assert body["total_message_ids"] == 2
    assert body["fetched"] == 1
    assert len(body["results"]) == 1
    assert body["results"][0]["status"] == "forwarded"
    assert body["results"][0]["direction"] == "Inbound"


def test_inbound_sms_payload_has_all_required_fields(app, client: TestClient):
    """
    SOW §3.3 Payload Requirements — verify every required field is present
    and correct in the ZapierPayload sent to the forwarder.

    Required fields:
      ✓ Inbound phone number (sender)          → from_number
      ✓ Phone number of RC user receiving SMS   → to_number
      ✓ SMS message body/content                → body
      ✓ Message ID                              → message_id
      ✓ Timestamp (UTC)                         → timestamp_utc
      ✓ Account/user ID                         → account_id
      ✓ Conversation/thread ID                  → conversation_id
      ✓ Delivery status                         → message_status
      ✓ Message direction flag                  → direction
      ✓ All available metadata                  → flat top-level fields
    """
    mock_forwarder = _make_mock_forwarder(status_code=200)
    _setup_app_state(app, messages=[RC_FULL_INBOUND_MESSAGE], forwarder=mock_forwarder)

    client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    # Inspect the ZapierPayload passed to forwarder.send()
    assert mock_forwarder.send.call_count == 1
    zapier_payload = mock_forwarder.send.call_args[0][0]

    # ── SOW Required Fields ──────────────────────────────────────
    # Inbound phone number (sender)
    assert zapier_payload.from_number == "+15550001111"
    assert zapier_payload.from_name == "John Doe"
    assert zapier_payload.from_location == "Irvine, CA"

    # Phone number of the RC user receiving the SMS
    assert zapier_payload.to_number == "+15559990000"
    assert zapier_payload.to_name == "RC User"
    assert zapier_payload.to_location == "New York, NY"

    # SMS message body/content
    assert zapier_payload.body == "Please remove me from your list"
    assert zapier_payload.subject == "Please remove me from your list"

    # Message ID and type
    assert zapier_payload.message_id == "3610703867026"
    assert zapier_payload.message_type == "SMS"

    # Timestamp (UTC)
    assert "2026-02-26" in zapier_payload.timestamp_utc
    assert zapier_payload.last_modified_utc is not None
    assert zapier_payload.sms_delivery_time_utc is not None
    assert zapier_payload.received_at_utc  # not empty

    # Account/user ID
    assert zapier_payload.account_id == "315079026"

    # Extension ID
    assert zapier_payload.extension_id == "2582602027"

    # Subscription ID
    assert zapier_payload.subscription_id == "7fd32175-9666-4dbc-9c7b-2af4c4f66cab"

    # Conversation/thread ID
    assert zapier_payload.conversation_id == "conv-abc123"

    # Delivery status
    assert zapier_payload.message_status == "Received"
    assert zapier_payload.read_status == "Unread"

    # Message direction flag
    assert zapier_payload.direction == "Inbound"
    assert zapier_payload.event_type == "inbound_sms"

    # Source
    assert zapier_payload.source == "ringcentral"

    # Priority / Availability
    assert zapier_payload.priority == "Normal"
    assert zapier_payload.availability == "Alive"

    # Message URI
    assert zapier_payload.message_uri is not None
    assert "message-store" in zapier_payload.message_uri

    # RC event metadata
    assert zapier_payload.rc_event_uuid == "47954648896411110707"
    assert "/message-store" in zapier_payload.rc_event_type

    # No raw_rc_payload — all data is in flat top-level fields
    assert not hasattr(zapier_payload, "raw_rc_payload") or "raw_rc_payload" not in zapier_payload.model_fields


# ─────────────────────────────────────────────────────────────────
# POST — Both Inbound AND Outbound forwarded
# ─────────────────────────────────────────────────────────────────

def test_both_inbound_and_outbound_forwarded(app, client: TestClient):
    """
    When RC notification contains 2 message IDs (one inbound, one outbound),
    BOTH should be fetched and forwarded to Zapier.
    """
    mock_forwarder = _make_mock_forwarder(status_code=200)
    _setup_app_state(
        app,
        messages=[RC_FULL_INBOUND_MESSAGE, RC_FULL_OUTBOUND_MESSAGE],
        forwarder=mock_forwarder,
    )

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processed"
    assert body["fetched"] == 2

    # Forwarder should have been called twice — once per message
    assert mock_forwarder.send.call_count == 2

    # Collect all forwarded payloads
    payloads = [call[0][0] for call in mock_forwarder.send.call_args_list]

    # One should be inbound, one should be outbound
    directions = {p.direction for p in payloads}
    assert "Inbound" in directions
    assert "Outbound" in directions

    event_types = {p.event_type for p in payloads}
    assert "inbound_sms" in event_types
    assert "outbound_sms" in event_types


def test_outbound_sms_payload_has_correct_fields(app, client: TestClient):
    """Verify outbound SMS payload contains all the right data."""
    mock_forwarder = _make_mock_forwarder(status_code=200)
    _setup_app_state(
        app,
        messages=[RC_FULL_OUTBOUND_MESSAGE],
        forwarder=mock_forwarder,
    )

    client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert mock_forwarder.send.call_count == 1
    payload = mock_forwarder.send.call_args[0][0]

    assert payload.direction == "Outbound"
    assert payload.event_type == "outbound_sms"
    assert payload.from_number == "+15559990000"  # RC user sending
    assert payload.from_name == "RC User"
    assert payload.to_number == "+15550001111"     # external recipient
    assert payload.to_name == "John Doe"
    assert payload.body == "Hi there, how can we help you?"
    assert payload.subject == "Hi there, how can we help you?"
    assert payload.message_id == "3610703807026"
    assert payload.message_type == "SMS"
    assert payload.message_status == "Sent"
    assert payload.read_status == "Read"


# ─────────────────────────────────────────────────────────────────
# POST — Notification with no SMS changes (e.g. Voicemail)
# ─────────────────────────────────────────────────────────────────

def test_non_sms_notification_ignored(app, client: TestClient):
    """When the notification changes[] has type=Voicemail, it should be ignored."""
    _setup_app_state(app)

    payload = {
        "uuid": "vm-uuid-001",
        "event": "/restapi/v1.0/account/315079026/extension/2582602027/message-store",
        "timestamp": "2026-02-26T21:14:57.833Z",
        "subscriptionId": "sub-001",
        "ownerId": "258260202",
        "body": {
            "accountId": 315079026,
            "extensionId": 2582602027,
            "lastUpdated": "2026-02-26T21:14:43.560Z",
            "changes": [
                {
                    "type": "Voicemail",
                    "newCount": 1,
                    "updatedCount": 0,
                    "newMessageIds": [999999999],
                }
            ],
        },
    }

    response = client.post(
        "/api/v1/rc/webhook",
        json=payload,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert "no_new_sms_message_ids" in response.json()["reason"]


def test_non_sms_message_type_filtered_after_fetch(app, client: TestClient):
    """If RC API returns a non-SMS message (e.g. Fax), it should be filtered out."""
    fax_message = dict(RC_FULL_INBOUND_MESSAGE)
    fax_message["type"] = "Fax"
    fax_message["id"] = 9999999

    mock_forwarder = _make_mock_forwarder()
    _setup_app_state(app, messages=[fax_message], forwarder=mock_forwarder)

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    # Forwarder should NOT have been called (fax was filtered)
    assert mock_forwarder.send.call_count == 0


# ─────────────────────────────────────────────────────────────────
# POST — Idempotency
# ─────────────────────────────────────────────────────────────────

def test_duplicate_message_suppressed(app, client: TestClient):
    """Same notification sent twice — second time the message should be deduplicated."""
    mock_forwarder = _make_mock_forwarder()
    cache = IdempotencyCache(maxsize=100, ttl=60)
    _setup_app_state(app, messages=[RC_FULL_INBOUND_MESSAGE], forwarder=mock_forwarder, cache=cache)

    # First POST — should forward
    r1 = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert r1.json()["status"] == "processed"
    assert r1.json()["results"][0]["status"] == "forwarded"

    # Second POST with same messageId — should be suppressed
    r2 = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert r2.json()["status"] == "processed"
    assert r2.json()["results"][0]["status"] == "duplicate"

    # Forwarder was called exactly once (first time only)
    assert mock_forwarder.send.call_count == 1


# ─────────────────────────────────────────────────────────────────
# POST — Zapier Forward Failure (all retries exhausted)
# ─────────────────────────────────────────────────────────────────

def test_zapier_forward_failure_returns_200_with_error_status(app, client: TestClient):
    """When all Zapier retries are exhausted, the endpoint should still return
    200 to RC (to prevent RC retries) but with status=forward_failed in results."""
    error = ZapierForwardError(
        "Zapier webhook failed after 3 attempts",
        attempts=3,
        last_status_code=500,
        message_id="3610703867026",
    )
    mock_forwarder = _make_mock_forwarder(raises=error)
    _setup_app_state(app, messages=[RC_FULL_INBOUND_MESSAGE], forwarder=mock_forwarder)

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processed"
    assert body["results"][0]["status"] == "forward_failed"
    assert body["results"][0]["message_id"] == "3610703867026"
    assert "Zapier unreachable" in body["results"][0]["detail"]


def test_zapier_forward_failure_does_not_mark_as_seen(app, client: TestClient):
    """Failed forwards should NOT mark the message as seen, allowing retry."""
    error = ZapierForwardError(
        "Zapier webhook failed",
        attempts=3,
        last_status_code=500,
        message_id="3610703867026",
    )
    mock_forwarder = _make_mock_forwarder(raises=error)
    cache = IdempotencyCache(maxsize=100, ttl=60)
    _setup_app_state(app, messages=[RC_FULL_INBOUND_MESSAGE], forwarder=mock_forwarder, cache=cache)

    client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    # The message should NOT be in the cache since forward failed
    assert cache.is_duplicate("3610703867026") is False


# ─────────────────────────────────────────────────────────────────
# POST — RC API fetch failure
# ─────────────────────────────────────────────────────────────────

def test_rc_api_returns_no_messages(app, client: TestClient):
    """If RC API fails to return any messages, return error status."""
    _setup_app_state(app, messages=[])  # Empty — RC API returned nothing

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "could_not_fetch_messages" in body["reason"]


# ─────────────────────────────────────────────────────────────────
# POST — Edge cases
# ─────────────────────────────────────────────────────────────────

def test_notification_without_account_or_extension_id(app, client: TestClient):
    """If accountId/extensionId can't be determined, return error."""
    _setup_app_state(app, messages=[RC_FULL_INBOUND_MESSAGE])

    payload = {
        "uuid": "test-uuid",
        "event": "/restapi/v1.0/some/unknown/path",
        "timestamp": "2026-02-26T21:14:57.833Z",
        "subscriptionId": "sub-001",
        "body": {
            "changes": [
                {
                    "type": "SMS",
                    "newCount": 1,
                    "updatedCount": 0,
                    "newMessageIds": [123456],
                }
            ],
        },
    }

    response = client.post(
        "/api/v1/rc/webhook",
        json=payload,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "missing_account_or_extension_id" in body["reason"]


def test_inbound_message_with_missing_optional_fields(app, client: TestClient):
    """Message without some optional fields should still forward correctly."""
    minimal_message = {
        "id": 7777777,
        "type": "SMS",
        "direction": "Inbound",
        "from": {"phoneNumber": "+15551112222"},
        "to": [{"phoneNumber": "+15553334444"}],
        "subject": "Stop texting me",
        "creationTime": "2026-02-26T22:00:00.000Z",
    }

    mock_forwarder = _make_mock_forwarder()
    _setup_app_state(app, messages=[minimal_message], forwarder=mock_forwarder)

    client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert mock_forwarder.send.call_count == 1
    payload = mock_forwarder.send.call_args[0][0]

    assert payload.from_number == "+15551112222"
    assert payload.to_number == "+15553334444"
    assert payload.body == "Stop texting me"
    assert payload.direction == "Inbound"
    assert payload.message_id == "7777777"
    # Optional fields should be None, not raise errors
    assert payload.conversation_id is None
    assert payload.read_status is None
    assert payload.delivery_error_code is None


# ─────────────────────────────────────────────────────────────────
# Duplicate notification (multi-worker simulation)
# ─────────────────────────────────────────────────────────────────

def test_same_notification_sent_twice_only_forwards_once(app, client: TestClient):
    """
    Simulates the 2-worker duplicate bug: the SAME RC notification arrives
    twice (same message IDs). The idempotency cache must ensure each message
    is forwarded to Zapier only ONCE — not doubled.
    """
    mock_forwarder = _make_mock_forwarder()
    cache = IdempotencyCache(maxsize=100, ttl=60)
    _setup_app_state(
        app,
        messages=[RC_FULL_INBOUND_MESSAGE, RC_FULL_OUTBOUND_MESSAGE],
        forwarder=mock_forwarder,
        cache=cache,
    )

    # First notification — should forward both inbound + outbound = 2 calls
    r1 = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert r1.status_code == 200
    body1 = r1.json()
    forwarded_1 = [r for r in body1["results"] if r["status"] == "forwarded"]
    assert len(forwarded_1) == 2  # inbound + outbound

    # Second IDENTICAL notification — same message IDs
    r2 = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    duplicates = [r for r in body2["results"] if r["status"] == "duplicate"]
    forwarded_2 = [r for r in body2["results"] if r["status"] == "forwarded"]
    assert len(duplicates) == 2   # both suppressed
    assert len(forwarded_2) == 0  # nothing new sent

    # Total: forwarder called exactly 2 times (first notification only)
    assert mock_forwarder.send.call_count == 2


def test_payload_has_no_raw_rc_payload_field(app, client: TestClient):
    """
    Zapier payload must be 100% flat — no raw_rc_payload blob.
    All data is in individual top-level fields.
    """
    mock_forwarder = _make_mock_forwarder()
    _setup_app_state(app, messages=[RC_FULL_INBOUND_MESSAGE], forwarder=mock_forwarder)

    client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    payload = mock_forwarder.send.call_args[0][0]
    payload_dict = payload.model_dump(mode="json")

    # raw_rc_payload must NOT be present
    assert "raw_rc_payload" not in payload_dict

    # All important fields must be flat top-level keys
    required_flat_fields = [
        "source", "event_type", "message_id", "message_type", "direction",
        "from_number", "to_number", "subject", "body",
        "timestamp_utc", "received_at_utc",
        "account_id", "extension_id", "conversation_id",
        "read_status", "message_status", "priority", "availability",
        "attachment_count", "message_uri", "rc_event_type", "rc_event_uuid",
    ]
    for field in required_flat_fields:
        assert field in payload_dict, f"Missing flat field: {field}"

    # Verify no nested dicts or lists in the payload (except None values)
    for key, value in payload_dict.items():
        assert not isinstance(value, (dict, list)), (
            f"Field '{key}' is nested ({type(value).__name__}), not flat!"
        )
