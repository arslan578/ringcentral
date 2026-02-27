"""
tests/test_zapier_forwarder.py

Unit tests for the ZapierForwarder service.
Verifies retry logic, exponential backoff, and ZapierForwardError behaviour.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.core.exceptions import ZapierForwardError
from app.schemas.zapier_payload import ZapierPayload
from app.services.zapier_forwarder import ForwardResult, ZapierForwarder


WEBHOOK_URL = "https://hooks.zapier.com/hooks/catch/test/"


def _sample_payload() -> ZapierPayload:
    return ZapierPayload(
        event_type="inbound_sms",
        message_id="test-msg-001",
        direction="Inbound",
        from_number="+15550001111",
        to_number="+15559990000",
        body="Please remove me",
        timestamp_utc="2026-02-25T00:59:50+00:00",
        received_at_utc="2026-02-25T01:00:00+00:00",
    )


def _make_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code=status_code, content=b"ok")


@pytest.mark.asyncio
async def test_success_on_first_attempt():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=_make_response(200))

    forwarder = ZapierForwarder(
        webhook_url=WEBHOOK_URL, http_client=mock_client, max_retries=3, base_delay=0.01
    )
    result = await forwarder.send(_sample_payload())

    assert result.success is True
    assert result.attempts == 1
    assert result.final_status_code == 200
    mock_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_retries_on_server_error_then_succeeds():
    """Fails twice (500), succeeds on 3rd attempt."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(
        side_effect=[
            _make_response(500),
            _make_response(503),
            _make_response(200),
        ]
    )

    forwarder = ZapierForwarder(
        webhook_url=WEBHOOK_URL, http_client=mock_client, max_retries=3, base_delay=0.01
    )

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await forwarder.send(_sample_payload())

    assert result.success is True
    assert result.attempts == 3
    assert result.final_status_code == 200
    assert mock_sleep.call_count == 2  # slept between attempt 1→2 and 2→3


@pytest.mark.asyncio
async def test_raises_after_all_retries_exhausted():
    """All 3 attempts return 500 — should raise ZapierForwardError."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=_make_response(500))

    forwarder = ZapierForwarder(
        webhook_url=WEBHOOK_URL, http_client=mock_client, max_retries=3, base_delay=0.01
    )

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(ZapierForwardError) as exc_info:
            await forwarder.send(_sample_payload())

    exc = exc_info.value
    assert exc.attempts == 3
    assert exc.last_status_code == 500
    assert mock_client.post.call_count == 3


@pytest.mark.asyncio
async def test_raises_on_network_error():
    """Network error on all attempts."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    forwarder = ZapierForwarder(
        webhook_url=WEBHOOK_URL, http_client=mock_client, max_retries=3, base_delay=0.01
    )

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(ZapierForwardError) as exc_info:
            await forwarder.send(_sample_payload())

    assert exc_info.value.attempts == 3
    assert exc_info.value.last_status_code is None


@pytest.mark.asyncio
async def test_exponential_backoff_delays():
    """Verify backoff delays: 0.01 → 0.02 (base_delay=0.01)."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=_make_response(500))

    forwarder = ZapierForwarder(
        webhook_url=WEBHOOK_URL, http_client=mock_client, max_retries=3, base_delay=1.0
    )

    sleep_calls = []

    async def capture_sleep(delay):
        sleep_calls.append(delay)

    with patch("asyncio.sleep", side_effect=capture_sleep):
        with pytest.raises(ZapierForwardError):
            await forwarder.send(_sample_payload())

    # Backoff: 1.0 * 2^0 = 1.0, 1.0 * 2^1 = 2.0
    assert sleep_calls == [1.0, 2.0]


@pytest.mark.asyncio
async def test_raises_on_timeout():
    """Timeout on all attempts should raise ZapierForwardError with None status code."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("Read timed out"))

    forwarder = ZapierForwarder(
        webhook_url=WEBHOOK_URL, http_client=mock_client, max_retries=3, base_delay=0.01
    )

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(ZapierForwardError) as exc_info:
            await forwarder.send(_sample_payload())

    assert exc_info.value.attempts == 3
    assert exc_info.value.last_status_code is None


@pytest.mark.asyncio
async def test_success_after_timeout_then_retry():
    """Timeout on first attempt, success on second."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(
        side_effect=[
            httpx.ReadTimeout("Read timed out"),
            _make_response(200),
        ]
    )

    forwarder = ZapierForwarder(
        webhook_url=WEBHOOK_URL, http_client=mock_client, max_retries=3, base_delay=0.01
    )

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await forwarder.send(_sample_payload())

    assert result.success is True
    assert result.attempts == 2
    assert result.final_status_code == 200