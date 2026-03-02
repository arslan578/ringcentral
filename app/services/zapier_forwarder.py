"""
app/services/zapier_forwarder.py

Async HTTP service that POSTs the full metadata payload to the Zapier webhook.

Reliability features (per SOW §3.4):
  - Minimum 3 retries with exponential backoff (1s → 2s → 4s)
  - Logs HTTP response codes and errors on every attempt
  - Raises ZapierForwardError only after all retries are exhausted
  - Returns a ForwardResult with success flag, attempts, and final status code

All HTTP calls go through a shared httpx.AsyncClient (injected from app state)
to benefit from connection pooling and keep-alive.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from app.core.exceptions import ZapierForwardError
from app.schemas.zapier_payload import ZapierPayload

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class ForwardResult:
    """Result of a Zapier forward attempt (successful or exhausted)."""
    success: bool
    attempts: int
    final_status_code: int | None
    message_id: str


class ZapierForwarder:
    """
    Stateless service wrapping the Zapier POST call with retry logic.
    Inject via `app.state.zapier_forwarder` (set at startup in main.py).
    """

    def __init__(
        self,
        webhook_url: str,
        http_client: httpx.AsyncClient,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ):
        self._webhook_url = webhook_url
        self._client = http_client
        self._max_retries = max_retries
        self._base_delay = base_delay

    async def send(
        self,
        payload: ZapierPayload,
        webhook_url: str | None = None,
    ) -> ForwardResult:
        """
        POST the payload to Zapier. Retries on HTTP errors (4xx/5xx) or
        network exceptions using exponential backoff.

        Args:
            payload:     The fully built ZapierPayload to transmit.
            webhook_url: Optional URL override. If provided, this URL is used
                         instead of the default set at construction time.
                         Use this to route inbound/outbound to different Zaps.

        Returns:
            ForwardResult on success.

        Raises:
            ZapierForwardError: if all retry attempts fail.
        """
        target_url = webhook_url or self._webhook_url
        message_id = payload.message_id
        last_status_code: int | None = None
        last_exception: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                logger.info(
                    "Sending payload to Zapier",
                    extra={
                        "event": "zapier_forward_attempt",
                        "message_id": message_id,
                        "attempt": attempt,
                        "max_retries": self._max_retries,
                        "from_number": payload.from_number,
                        "to_number": payload.to_number,
                    },
                )

                response = await self._client.post(
                    target_url,
                    json=payload.model_dump(mode="json"),
                    timeout=15.0,
                )

                last_status_code = response.status_code

                if response.is_success:
                    logger.info(
                        "Zapier webhook accepted payload",
                        extra={
                            "event": "zapier_forward_success",
                            "message_id": message_id,
                            "attempt": attempt,
                            "zapier_status_code": last_status_code,
                        },
                    )
                    return ForwardResult(
                        success=True,
                        attempts=attempt,
                        final_status_code=last_status_code,
                        message_id=message_id,
                    )

                # Non-2xx response — log and retry
                logger.warning(
                    "Zapier returned non-success status",
                    extra={
                        "event": "zapier_forward_retry",
                        "message_id": message_id,
                        "attempt": attempt,
                        "zapier_status_code": last_status_code,
                        "response_body": response.text[:500],
                    },
                )

            except httpx.TimeoutException as exc:
                last_exception = exc
                logger.warning(
                    "Zapier request timed out",
                    extra={
                        "event": "zapier_forward_timeout",
                        "message_id": message_id,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )

            except httpx.RequestError as exc:
                last_exception = exc
                logger.warning(
                    "Zapier network error",
                    extra={
                        "event": "zapier_forward_network_error",
                        "message_id": message_id,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )

            # Don't sleep after the last attempt
            if attempt < self._max_retries:
                delay = self._base_delay * (2 ** (attempt - 1))
                logger.debug(
                    "Backoff before retry",
                    extra={
                        "message_id": message_id,
                        "backoff_seconds": delay,
                        "next_attempt": attempt + 1,
                    },
                )
                await asyncio.sleep(delay)

        # All retries exhausted
        error_msg = (
            f"Zapier webhook failed after {self._max_retries} attempts "
            f"for message_id={message_id}. "
            f"Last status: {last_status_code}. "
            f"Last error: {last_exception}"
        )
        logger.error(
            "All Zapier retry attempts exhausted",
            extra={
                "event": "zapier_forward_failed",
                "message_id": message_id,
                "total_attempts": self._max_retries,
                "last_status_code": last_status_code,
                "last_error": str(last_exception) if last_exception else None,
            },
        )
        raise ZapierForwardError(
            error_msg,
            attempts=self._max_retries,
            last_status_code=last_status_code,
            message_id=message_id,
        )
