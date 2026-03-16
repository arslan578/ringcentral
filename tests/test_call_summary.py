"""
tests/test_call_summary.py

End-to-end tests for the call-ended AI notes → Logics endpoint feature.

Tests cover:
  1. Call-ended event detected and routed to CallSummaryHandler
  2. Correct payload fields (subject format, body = notes, caller number, etc.)
  3. Retry logic when notes are not immediately available
  4. Fallback when notes never arrive
  5. SMS path unaffected (regression)
  6. Logics POST failure handled gracefully (200 still returned to RC)
  7. Deduplication: second Disconnected event for same session is skipped
  8. Status filtering: only Disconnected triggers handler
  9. Agent name filtering: IVR/queue names are skipped
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.core.idempotency import IdempotencyCache
from app.services.call_summary_handler import (
    CallSummaryHandler,
    _extract_call_info,
    _extract_notes_from_call_log,
    _is_real_agent_name,
)
from app.schemas.call_summary_payload import CallSummaryPayload
from app.services.rc_api_client import RCApiClient
from app.services.redaction import SensitiveDataRedactor
from app.services.zapier_forwarder import ForwardResult, ZapierForwarder
from tests.conftest import (
    RC_NOTIFICATION_PAYLOAD,
    RC_FULL_INBOUND_MESSAGE,
    VALID_TOKEN,
    make_mock_rc_api_client,
)

# ─────────────────────────────────────────────────────────────────
# Sample RC telephony/sessions call-ended webhook payload
# This is what RC sends to the webhook when a call ends.
# ─────────────────────────────────────────────────────────────────

RC_CALL_ENDED_PAYLOAD = {
    "uuid": "call-uuid-001",
    "event": "/restapi/v1.0/account/315079026/telephony/sessions",
    "timestamp": "2026-03-14T17:00:05.000Z",
    "subscriptionId": "7fd32175-9666-4dbc-9c7b-2af4c4f66cab",
    "ownerId": "315079026",
    "body": {
        "accountId": "315079026",
        "telephonySessionId": "s-call-session-abc123",
        "sessionId": "s-call-session-abc123",
        "parties": [
            {
                "accountId": "315079026",
                "extensionId": "2582602027",
                "id": "party-agent-001",
                "direction": "Inbound",
                "status": {"code": "Disconnected"},
                "muted": False,
                "from": {"phoneNumber": "+15550001111", "name": "John Doe"},
                "to": {"phoneNumber": "+15559990000", "name": "Jane Smith"},
            }
        ],
    },
}

# A simulated RC Call Log API response with AI notes
RC_CALL_LOG_WITH_NOTES = {
    "id": "s-call-session-abc123",
    "uri": "https://platform.ringcentral.com/restapi/v1.0/account/315079026/call-log/s-call-session-abc123",
    "duration": 185,
    "startTime": "2026-03-14T17:00:01.000Z",
    "direction": "Inbound",
    "type": "Voice",
    "action": "Phone Call",
    "notes": (
        "Customer John Doe called about their account balance inquiry. "
        "Confirmed identity and provided balance details. "
        "Customer satisfied with the response. No further action needed."
    ),
    "from": {"phoneNumber": "+15550001111", "name": "John Doe"},
    "to": {"phoneNumber": "+15559990000", "name": "Jane Smith"},
    "legs": [
        {
            "direction": "Inbound",
            "from": {"phoneNumber": "+15550001111", "name": "John Doe"},
            "to": {"phoneNumber": "+15559990000", "name": "Jane Smith"},
            "duration": 185,
        }
    ],
}

RC_CALL_LOG_NO_NOTES = {
    "id": "s-call-session-abc123",
    "duration": 185,
    "startTime": "2026-03-14T17:00:01.000Z",
    "direction": "Inbound",
    "type": "Voice",
    "notes": "",
    "from": {"phoneNumber": "+15550001111", "name": "John Doe"},
    "to": {"phoneNumber": "+15559990000", "name": "Jane Smith"},
}


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _make_mock_rc_api_with_call_log(
    call_log: dict | None = None,
    sms_messages: list | None = None,
) -> RCApiClient:
    """Mock RCApiClient that returns a specific call log entry."""
    mock = MagicMock(spec=RCApiClient)
    mock.get_call_log_entry = AsyncMock(return_value=call_log)
    mock.get_messages_batch = AsyncMock(return_value=sms_messages or [])
    mock.get_message = AsyncMock(return_value=None)
    return mock


def _make_mock_http_client(status_code: int = 200) -> MagicMock:
    """Mock httpx.AsyncClient whose POST returns the given status."""
    response_mock = MagicMock()
    response_mock.status_code = status_code
    response_mock.is_success = (200 <= status_code < 300)
    response_mock.text = "ok"
    client = MagicMock()
    client.post = AsyncMock(return_value=response_mock)
    return client


def _make_mock_forwarder() -> ZapierForwarder:
    """Mock ZapierForwarder for SMS tests."""
    forwarder = MagicMock(spec=ZapierForwarder)
    forwarder.send = AsyncMock(
        return_value=ForwardResult(
            success=True, attempts=1, final_status_code=200, message_id="3610703867026"
        )
    )
    return forwarder


def _setup_call_app_state(
    app,
    call_log=None,
    logics_status: int = 200,
    sms_messages=None,
):
    """Wire up app state for call-ended tests."""
    mock_rc_api = _make_mock_rc_api_with_call_log(call_log, sms_messages)
    mock_http = _make_mock_http_client(logics_status)

    handler = CallSummaryHandler(
        rc_api=mock_rc_api,
        http_client=mock_http,  # type: ignore[arg-type]
        logics_url="https://hooks.logics.test/call-summary/",
        retry_schedule=[0],  # single immediate attempt for tests (no waiting)
    )

    app.state.rc_api_client = mock_rc_api
    app.state.call_summary_handler = handler
    app.state.zapier_forwarder = _make_mock_forwarder()
    app.state.idempotency_cache = IdempotencyCache(maxsize=100, ttl=60)
    app.state.redactor = SensitiveDataRedactor(enabled=False)
    return handler, mock_http


# ─────────────────────────────────────────────────────────────────
# Unit tests: _is_real_agent_name
# ─────────────────────────────────────────────────────────────────

def test_real_agent_name():
    """Real person names return True."""
    assert _is_real_agent_name("Jane Smith") is True
    assert _is_real_agent_name("Gilbert Paa") is True
    assert _is_real_agent_name("Marisol Contrer") is True


def test_ivr_names_are_not_agents():
    """IVR/queue names return False."""
    assert _is_real_agent_name("Main Company Number") is False
    assert _is_real_agent_name("1d. Billing Department") is False
    assert _is_real_agent_name("Auto Receptionist") is False
    assert _is_real_agent_name("IVR") is False
    assert _is_real_agent_name(None) is False
    assert _is_real_agent_name("") is False


# ─────────────────────────────────────────────────────────────────
# Unit tests: _extract_call_info
# ─────────────────────────────────────────────────────────────────

def test_extract_call_info_inbound():
    """Extract call_id and agent from an inbound call-ended payload."""
    (
        account_id, call_id, agent_name, agent_number,
        caller_number, caller_name, direction, _, __,
    ) = _extract_call_info(RC_CALL_ENDED_PAYLOAD)

    assert account_id == "315079026"
    assert call_id == "s-call-session-abc123"
    assert agent_name == "Jane Smith"          # To side = RC agent (inbound)
    assert caller_number == "+15550001111"     # From side = external caller
    assert caller_name == "John Doe"
    assert direction == "Inbound"


def test_extract_call_info_outbound():
    """Extract call_id and agent from an outbound call-ended payload."""
    payload = {
        "event": "/restapi/v1.0/account/315079026/telephony/sessions",
        "ownerId": "315079026",
        "body": {
            "accountId": "315079026",
            "telephonySessionId": "s-out-abc",
            "parties": [
                {
                    "accountId": "315079026",
                    "extensionId": "9999",
                    "direction": "Outbound",
                    "from": {"phoneNumber": "+15559990000", "name": "Agent Bob"},
                    "to": {"phoneNumber": "+15551112222", "name": "Dave"},
                }
            ],
        },
    }
    _, call_id, agent_name, _, caller_number, caller_name, direction, _, __ = (
        _extract_call_info(payload)
    )
    assert call_id == "s-out-abc"
    assert agent_name == "Agent Bob"
    assert caller_number == "+15551112222"
    assert caller_name == "Dave"
    assert direction == "Outbound"


def test_extract_call_info_skips_ivr_prefers_real_agent():
    """When multiple parties exist, IVR names are skipped in favor of real agents."""
    payload = {
        "event": "/restapi/v1.0/account/123/telephony/sessions",
        "body": {
            "accountId": "123",
            "telephonySessionId": "s-multi",
            "parties": [
                {
                    "extensionId": "1001",
                    "direction": "Inbound",
                    "status": {"code": "Disconnected"},
                    "from": {"phoneNumber": "+15550001111", "name": "Caller"},
                    "to": {"phoneNumber": "+18880001111", "name": "Main Company Number"},
                },
                {
                    "extensionId": "2002",
                    "direction": "Inbound",
                    "status": {"code": "Disconnected"},
                    "from": {"phoneNumber": "+15550001111", "name": "Caller"},
                    "to": {"phoneNumber": "+15559990000", "name": "Sarah Johnson"},
                },
            ],
        },
    }
    _, _, agent_name, _, caller_number, caller_name, _, _, __ = _extract_call_info(payload)
    assert agent_name == "Sarah Johnson"      # Real agent, not "Main Company Number"
    assert caller_number == "+15550001111"


def test_extract_call_info_missing_session_id():
    """Returns empty call_id when payload has no session ID fields."""
    payload = {
        "event": "/restapi/v1.0/account/123/telephony/sessions",
        "body": {"accountId": "123", "parties": []},
    }
    _, call_id, *_ = _extract_call_info(payload)
    assert call_id == ""


# ─────────────────────────────────────────────────────────────────
# Unit tests: _extract_notes_from_call_log
# ─────────────────────────────────────────────────────────────────

def test_extract_notes_from_call_log_notes_field():
    """Primary notes field is returned."""
    assert "Customer John" in _extract_notes_from_call_log(RC_CALL_LOG_WITH_NOTES)


def test_extract_notes_fallback_to_ai_notes():
    """Falls through to aiNotes field when notes is empty."""
    log = {"notes": "", "aiNotes": "AI-generated summary here"}
    assert _extract_notes_from_call_log(log) == "AI-generated summary here"


def test_extract_notes_fallback_to_transcription():
    """Falls through to transcription.text when both notes fields are empty."""
    log = {"notes": "", "aiNotes": "", "transcription": {"text": "Transcript text"}}
    assert _extract_notes_from_call_log(log) == "Transcript text"


def test_extract_notes_returns_empty_when_nothing_available():
    """All fields absent → returns empty string."""
    assert _extract_notes_from_call_log({}) == ""
    assert _extract_notes_from_call_log({"notes": "  "}) == ""


# ─────────────────────────────────────────────────────────────────
# Unit tests: CallSummaryPayload.build
# ─────────────────────────────────────────────────────────────────

def test_call_summary_payload_subject_format():
    """Subject should follow 'RingCentral Call Summary By: {name}' format."""
    payload = CallSummaryPayload.build(
        call_id="call-001",
        agent_name="Jane Smith",
        agent_number="+15559990000",
        caller_number="+15550001111",
        caller_name="John Doe",
        call_direction="Inbound",
        call_duration_seconds=185,
        call_datetime_utc="2026-03-14T17:00:01.000Z",
        notes="The customer asked about their balance.",
    )
    assert payload.subject == "RingCentral Call Summary By: Jane Smith"
    assert payload.body == "The customer asked about their balance."
    assert payload.caller_number == "+15550001111"
    assert payload.call_duration_seconds == 185
    assert payload.call_datetime_utc == "2026-03-14T17:00:01.000Z"
    assert payload.event_type == "call_ended"
    assert payload.source == "ringcentral_call"


def test_call_summary_payload_no_notes_fallback():
    """When notes is empty, body should be the fallback message."""
    payload = CallSummaryPayload.build(
        call_id="call-002",
        agent_name="Jane Smith",
        agent_number=None,
        caller_number="+15550001111",
        caller_name=None,
        call_direction=None,
        call_duration_seconds=None,
        call_datetime_utc=None,
        notes="",
    )
    assert payload.body == "(AI notes not available)"


def test_call_summary_payload_subject_uses_number_fallback():
    """When agent_name is None, subject falls back to agent_number."""
    payload = CallSummaryPayload.build(
        call_id="call-003",
        agent_name=None,
        agent_number="+15559990000",
        caller_number="+15550001111",
        caller_name=None,
        call_direction="Outbound",
        call_duration_seconds=60,
        call_datetime_utc=None,
        notes="Some notes",
    )
    assert payload.subject == "RingCentral Call Summary By: +15559990000"


def test_call_summary_payload_is_flat():
    """Payload must not contain nested dicts or lists — all fields are flat."""
    payload = CallSummaryPayload.build(
        call_id="call-004",
        agent_name="Agent",
        agent_number="+1555",
        caller_number="+1556",
        caller_name=None,
        call_direction="Inbound",
        call_duration_seconds=120,
        call_datetime_utc="2026-03-14T17:00:00Z",
        notes="Notes text",
    )
    data = payload.model_dump(mode="json")
    for key, value in data.items():
        assert not isinstance(value, (dict, list)), (
            f"Field '{key}' is nested ({type(value).__name__}), not flat!"
        )


# ─────────────────────────────────────────────────────────────────
# Integration tests: webhook endpoint routes call-ended events
# ─────────────────────────────────────────────────────────────────

def test_call_ended_event_returns_200(app, client: TestClient):
    """Call-ended webhook returns 200 and call_summary_queued status."""
    _setup_call_app_state(app, call_log=RC_CALL_LOG_WITH_NOTES)

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ENDED_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "call_summary_queued"


def test_call_ended_event_calls_logics_endpoint(app, client: TestClient):
    """Call-ended webhook triggers a POST to the Logics endpoint."""
    _, mock_http = _setup_call_app_state(app, call_log=RC_CALL_LOG_WITH_NOTES)

    client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ENDED_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    # Logics POST must have been called
    assert mock_http.post.call_count == 1
    call_kwargs = mock_http.post.call_args
    posted_url = call_kwargs[0][0]
    assert "logics.test" in posted_url


def test_call_ended_payload_has_correct_fields(app, client: TestClient):
    """Verify the payload POSTed to Logics has the right structure and values."""
    _, mock_http = _setup_call_app_state(app, call_log=RC_CALL_LOG_WITH_NOTES)

    client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ENDED_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert mock_http.post.call_count == 1
    # Extract the JSON dict that was POSTed
    payload_dict = mock_http.post.call_args[1]["json"]

    # Subject format
    assert payload_dict["subject"].startswith("RingCentral Call Summary By:")
    assert "Jane Smith" in payload_dict["subject"]

    # Body (AI notes)
    assert "Customer John Doe" in payload_dict["body"]

    # Caller metadata
    assert payload_dict["caller_number"] == "+15550001111"
    assert payload_dict["call_duration_seconds"] == 185
    assert payload_dict["call_datetime_utc"] == "2026-03-14T17:00:01.000Z"

    # Event / source
    assert payload_dict["event_type"] == "call_ended"
    assert payload_dict["source"] == "ringcentral_call"
    assert payload_dict["call_id"] == "s-call-session-abc123"


def test_call_ended_does_not_trigger_sms_forwarding(app, client: TestClient):
    """A call-ended event should NOT reach the SMS forwarding logic."""
    _, mock_http = _setup_call_app_state(app, call_log=RC_CALL_LOG_WITH_NOTES)
    # The SMS forwarder must NOT be called
    sms_forwarder = app.state.zapier_forwarder

    client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ENDED_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    sms_forwarder.send.assert_not_called()


# ─────────────────────────────────────────────────────────────────
# Deduplication tests
# ─────────────────────────────────────────────────────────────────

def test_duplicate_disconnected_event_is_skipped(app, client: TestClient):
    """Second Disconnected event for the same session is skipped (dedup)."""
    _, mock_http = _setup_call_app_state(app, call_log=RC_CALL_LOG_WITH_NOTES)

    # First call → queued for background processing
    resp1 = client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ENDED_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert resp1.json()["status"] == "call_summary_queued"
    assert mock_http.post.call_count == 1

    # Second call with same session ID → duplicate skipped
    resp2 = client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ENDED_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert resp2.json()["status"] == "duplicate_call_skipped"
    # Logics POST was NOT called again
    assert mock_http.post.call_count == 1


def test_different_sessions_are_both_processed(app, client: TestClient):
    """Different session IDs with different phone numbers are processed independently."""
    _, mock_http = _setup_call_app_state(app, call_log=RC_CALL_LOG_WITH_NOTES)

    # First session
    client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ENDED_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    # Different session with DIFFERENT phone numbers (truly different call)
    payload2 = {
        **RC_CALL_ENDED_PAYLOAD,
        "uuid": "call-uuid-002",
        "body": {
            "accountId": "315079026",
            "telephonySessionId": "s-different-session",
            "sessionId": "s-different-session",
            "parties": [
                {
                    "accountId": "315079026",
                    "extensionId": "2582602027",
                    "id": "party-agent-002",
                    "direction": "Inbound",
                    "status": {"code": "Disconnected"},
                    "muted": False,
                    "from": {"phoneNumber": "+15553334444", "name": "Alice"},
                    "to": {"phoneNumber": "+15557778888", "name": "Bob Agent"},
                }
            ],
        },
    }
    client.post(
        "/api/v1/rc/webhook",
        json=payload2,
        headers={"Verification-Token": VALID_TOKEN},
    )

    # Both should have been processed (different calls)
    assert mock_http.post.call_count == 2


def test_same_phones_different_sessions_are_deduped(app, client: TestClient):
    """Same phone pair but different session IDs (IVR/queue legs) are deduped."""
    _, mock_http = _setup_call_app_state(app, call_log=RC_CALL_LOG_WITH_NOTES)

    # First session — processed
    resp1 = client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ENDED_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )
    assert resp1.json()["status"] == "call_summary_queued"

    # Different session ID but SAME phone numbers (IVR/queue/agent legs)
    payload2 = {
        **RC_CALL_ENDED_PAYLOAD,
        "uuid": "call-uuid-002",
        "body": {
            **RC_CALL_ENDED_PAYLOAD["body"],
            "telephonySessionId": "s-different-session-same-call",
            "sessionId": "s-different-session-same-call",
        },
    }
    resp2 = client.post(
        "/api/v1/rc/webhook",
        json=payload2,
        headers={"Verification-Token": VALID_TOKEN},
    )

    # Second should be caught by phone-pair dedup
    assert resp2.json()["status"] == "duplicate_call_skipped"
    # Only ONE Logics POST
    assert mock_http.post.call_count == 1


# ─────────────────────────────────────────────────────────────────
# Retry logic tests
# ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notes_not_ready_triggers_retry():
    """
    When the first call log fetch returns no notes, the handler retries.
    On the second attempt, notes become available.
    """
    mock_rc_api = MagicMock(spec=RCApiClient)
    # First call: no notes. Second call: notes available.
    mock_rc_api.get_call_log_entry = AsyncMock(
        side_effect=[RC_CALL_LOG_NO_NOTES, RC_CALL_LOG_WITH_NOTES]
    )

    mock_http = _make_mock_http_client(200)

    handler = CallSummaryHandler(
        rc_api=mock_rc_api,
        http_client=mock_http,  # type: ignore[arg-type]
        logics_url="https://hooks.logics.test/call-summary/",
        retry_schedule=[0, 0],  # two attempts, no waiting
    )

    result = await handler.handle(RC_CALL_ENDED_PAYLOAD)

    # Should have called the RC API twice (initial + retry)
    assert mock_rc_api.get_call_log_entry.call_count == 2
    # Logics POST should have been made
    assert mock_http.post.call_count == 1
    # Result indicates success
    assert result["status"] == "sent"

    # The POSTed payload should have the notes text, not the fallback
    payload_json = mock_http.post.call_args[1]["json"]
    assert "Customer John" in payload_json["body"]
    assert payload_json["notes_retry_attempted"] is True


@pytest.mark.asyncio
async def test_notes_never_ready_sends_fallback():
    """
    If notes are empty after all attempts, the handler sends a summary
    with body='(AI notes not available)' — it does NOT drop the event.
    """
    mock_rc_api = MagicMock(spec=RCApiClient)
    # All calls return empty notes
    mock_rc_api.get_call_log_entry = AsyncMock(
        side_effect=[RC_CALL_LOG_NO_NOTES, RC_CALL_LOG_NO_NOTES, RC_CALL_LOG_NO_NOTES]
    )

    mock_http = _make_mock_http_client(200)

    handler = CallSummaryHandler(
        rc_api=mock_rc_api,
        http_client=mock_http,  # type: ignore[arg-type]
        logics_url="https://hooks.logics.test/call-summary/",
        retry_schedule=[0, 0, 0],  # three attempts, no waiting
    )

    result = await handler.handle(RC_CALL_ENDED_PAYLOAD)

    assert mock_rc_api.get_call_log_entry.call_count == 3
    assert mock_http.post.call_count == 1
    payload_json = mock_http.post.call_args[1]["json"]
    assert payload_json["body"] == "(AI notes not available)"
    assert payload_json["notes_retry_attempted"] is True


@pytest.mark.asyncio
async def test_notes_ready_on_first_try_no_retry():
    """When notes are ready immediately, only one RC API call is made."""
    mock_rc_api = MagicMock(spec=RCApiClient)
    mock_rc_api.get_call_log_entry = AsyncMock(return_value=RC_CALL_LOG_WITH_NOTES)

    mock_http = _make_mock_http_client(200)

    handler = CallSummaryHandler(
        rc_api=mock_rc_api,
        http_client=mock_http,  # type: ignore[arg-type]
        logics_url="https://hooks.logics.test/call-summary/",
        retry_schedule=[0],  # single attempt
    )

    result = await handler.handle(RC_CALL_ENDED_PAYLOAD)

    # Only one RC API call — no retry needed
    assert mock_rc_api.get_call_log_entry.call_count == 1
    assert result["status"] == "sent"

    payload_json = mock_http.post.call_args[1]["json"]
    assert payload_json["notes_retry_attempted"] is False


@pytest.mark.asyncio
async def test_api_failure_then_success():
    """When the first API call returns None (failure), the handler retries."""
    mock_rc_api = MagicMock(spec=RCApiClient)
    # First call: API failure (returns None). Second call: success.
    mock_rc_api.get_call_log_entry = AsyncMock(
        side_effect=[None, RC_CALL_LOG_WITH_NOTES]
    )

    mock_http = _make_mock_http_client(200)

    handler = CallSummaryHandler(
        rc_api=mock_rc_api,
        http_client=mock_http,  # type: ignore[arg-type]
        logics_url="https://hooks.logics.test/call-summary/",
        retry_schedule=[0, 0],  # two attempts, no waiting
    )

    result = await handler.handle(RC_CALL_ENDED_PAYLOAD)

    assert mock_rc_api.get_call_log_entry.call_count == 2
    assert result["status"] == "sent"
    payload_json = mock_http.post.call_args[1]["json"]
    assert "Customer John" in payload_json["body"]


# ─────────────────────────────────────────────────────────────────
# Error handling tests
# ─────────────────────────────────────────────────────────────────

def test_logics_endpoint_failure_still_returns_200_to_rc(app, client: TestClient):
    """If Logics POST fails (5xx), we still return 200 to RC (so RC doesn't retry)."""
    _setup_call_app_state(
        app,
        call_log=RC_CALL_LOG_WITH_NOTES,
        logics_status=500,
    )

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ENDED_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    # RC must get 200 regardless of Logics failure
    # (handler runs in background, RC gets instant 200)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "call_summary_queued"


@pytest.mark.asyncio
async def test_missing_call_id_is_skipped():
    """If the payload has no session ID, handler skips gracefully."""
    payload_no_id = {
        "event": "/restapi/v1.0/account/123/telephony/sessions",
        "body": {
            "accountId": "123",
            # No telephonySessionId / sessionId
            "parties": [],
        },
    }

    mock_rc_api = MagicMock(spec=RCApiClient)
    mock_rc_api.get_call_log_entry = AsyncMock(return_value=None)
    mock_http = _make_mock_http_client(200)

    handler = CallSummaryHandler(
        rc_api=mock_rc_api,
        http_client=mock_http,  # type: ignore[arg-type]
        logics_url="https://hooks.logics.test/",
        retry_schedule=[0],
    )

    result = await handler.handle(payload_no_id)
    assert result["status"] == "skipped"
    assert result["reason"] == "no_call_id"
    # No RC API call and no Logics POST
    mock_rc_api.get_call_log_entry.assert_not_called()
    mock_http.post.assert_not_called()


@pytest.mark.asyncio
async def test_no_logics_url_logs_and_skips():
    """If LOGICS_WEBHOOK_URL is empty, handler logs a warning and skips the POST."""
    mock_rc_api = MagicMock(spec=RCApiClient)
    mock_rc_api.get_call_log_entry = AsyncMock(return_value=RC_CALL_LOG_WITH_NOTES)
    mock_http = _make_mock_http_client(200)

    handler = CallSummaryHandler(
        rc_api=mock_rc_api,
        http_client=mock_http,  # type: ignore[arg-type]
        logics_url="",  # Not configured
        retry_schedule=[0],
    )

    result = await handler.handle(RC_CALL_ENDED_PAYLOAD)
    assert result["status"] == "skipped"
    assert result["reason"] == "no_logics_url"
    mock_http.post.assert_not_called()


# ─────────────────────────────────────────────────────────────────
# Regression tests: existing SMS path unaffected
# ─────────────────────────────────────────────────────────────────

def test_sms_notification_still_forwarded_after_feature_added(app, client: TestClient):
    """
    Regression: adding call-ended handler must not break SMS processing.
    An SMS notification (message-store event) should still forward to Zapier.
    """
    # Wire up the call summary handler alongside the forwarder
    mock_rc_api = make_mock_rc_api_client(messages=[RC_FULL_INBOUND_MESSAGE])
    mock_http = _make_mock_http_client(200)
    sms_forwarder = MagicMock(spec=ZapierForwarder)
    sms_forwarder.send = AsyncMock(
        return_value=ForwardResult(
            success=True, attempts=1, final_status_code=200, message_id="3610703867026"
        )
    )

    handler = CallSummaryHandler(
        rc_api=mock_rc_api,
        http_client=mock_http,  # type: ignore[arg-type]
        logics_url="https://hooks.logics.test/",
        retry_schedule=[0],
    )

    app.state.rc_api_client = mock_rc_api
    app.state.call_summary_handler = handler
    app.state.zapier_forwarder = sms_forwarder
    app.state.idempotency_cache = IdempotencyCache(maxsize=100, ttl=60)
    app.state.redactor = SensitiveDataRedactor(enabled=False)

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_NOTIFICATION_PAYLOAD,  # SMS notification (message-store path)
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processed"
    # SMS forwarder WAS called (SMS still works)
    assert sms_forwarder.send.call_count == 1
    # Logics endpoint WAS NOT called (not a call-ended event)
    mock_http.post.assert_not_called()


# ─────────────────────────────────────────────────────────────────
# Telephony status filtering tests
# ─────────────────────────────────────────────────────────────────

# Payload where the call is ringing (not ended yet)
RC_CALL_RINGING_PAYLOAD = {
    "uuid": "call-uuid-ring",
    "event": "/restapi/v1.0/account/315079026/telephony/sessions",
    "timestamp": "2026-03-14T17:00:01.000Z",
    "subscriptionId": "7fd32175-9666-4dbc-9c7b-2af4c4f66cab",
    "ownerId": "315079026",
    "body": {
        "accountId": "315079026",
        "telephonySessionId": "s-call-session-abc123",
        "sessionId": "s-call-session-abc123",
        "parties": [
            {
                "accountId": "315079026",
                "extensionId": "2582602027",
                "id": "party-agent-001",
                "direction": "Inbound",
                "status": {"code": "Proceeding"},
                "from": {"phoneNumber": "+15550001111", "name": "John Doe"},
                "to": {"phoneNumber": "+15559990000", "name": "Jane Smith"},
            }
        ],
    },
}

# Payload where the call is answered/connected (not ended yet)
RC_CALL_ANSWERED_PAYLOAD = {
    "uuid": "call-uuid-answered",
    "event": "/restapi/v1.0/account/315079026/telephony/sessions",
    "timestamp": "2026-03-14T17:00:02.000Z",
    "subscriptionId": "7fd32175-9666-4dbc-9c7b-2af4c4f66cab",
    "ownerId": "315079026",
    "body": {
        "accountId": "315079026",
        "telephonySessionId": "s-call-session-abc123",
        "sessionId": "s-call-session-abc123",
        "parties": [
            {
                "accountId": "315079026",
                "extensionId": "2582602027",
                "id": "party-agent-001",
                "direction": "Inbound",
                "status": {"code": "Answered"},
                "from": {"phoneNumber": "+15550001111", "name": "John Doe"},
                "to": {"phoneNumber": "+15559990000", "name": "Jane Smith"},
            }
        ],
    },
}


def test_ringing_event_is_skipped(app, client: TestClient):
    """Telephony event with Proceeding (ringing) should be skipped, NOT sent to Logics."""
    _, mock_http = _setup_call_app_state(app, call_log=RC_CALL_LOG_WITH_NOTES)

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_RINGING_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "telephony_event_skipped"
    # Handler was NOT called (no Logics POST)
    mock_http.post.assert_not_called()


def test_answered_event_is_skipped(app, client: TestClient):
    """Telephony event with Answered should be skipped, NOT sent to Logics."""
    _, mock_http = _setup_call_app_state(app, call_log=RC_CALL_LOG_WITH_NOTES)

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ANSWERED_PAYLOAD,
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "telephony_event_skipped"
    mock_http.post.assert_not_called()


def test_disconnected_event_is_processed(app, client: TestClient):
    """Only Disconnected status triggers the full call summary handler."""
    _, mock_http = _setup_call_app_state(app, call_log=RC_CALL_LOG_WITH_NOTES)

    response = client.post(
        "/api/v1/rc/webhook",
        json=RC_CALL_ENDED_PAYLOAD,  # Has status: Disconnected
        headers={"Verification-Token": VALID_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "call_summary_queued"
    # Logics POST WAS made (BackgroundTasks complete before TestClient returns)
    assert mock_http.post.call_count == 1
