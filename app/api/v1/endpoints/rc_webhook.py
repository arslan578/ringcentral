"""
app/api/v1/endpoints/rc_webhook.py

The primary endpoint: receives all inbound SMS webhook pushes from RingCentral.

Two operations on the same path /api/v1/rc/webhook:

  GET  — RC validation challenge (legacy/alternate).
         RC sends a GET with ?validationToken=<token>.
         We MUST echo it back as plain text with 200 OK.

  POST — Two sub-cases:
         a) Validation challenge: RC sends a POST with a "Validation-Token"
            header when creating/renewing a subscription. We echo the token
            back in the response "Validation-Token" header with 200 OK.
         b) Inbound SMS notification: RC posts the full event payload.
            We validate, deduplicate, build ZapierPayload, and forward.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from app.config import Settings, get_settings
from app.core.exceptions import (
    DuplicateMessageError,
    PayloadParseError,
    RCValidationError,
    ZapierForwardError,
)
from app.core.idempotency import IdempotencyCache
from app.core.rc_validator import validate_verification_token
from app.schemas.rc_message import RCWebhookEvent
from app.schemas.zapier_payload import ZapierPayload
from app.services.zapier_forwarder import ZapierForwarder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rc", tags=["RingCentral Webhook"])


def _get_forwarder(request: Request) -> ZapierForwarder:
    return request.app.state.zapier_forwarder


def _get_idempotency_cache(request: Request) -> IdempotencyCache:
    return request.app.state.idempotency_cache


@router.get(
    "/webhook",
    summary="RC Webhook Validation Challenge",
    description=(
        "RingCentral sends a GET request with ?validationToken=<token> when the "
        "webhook subscription is first registered. This endpoint echoes that token "
        "back as plain text to confirm ownership."
    ),
    response_class=PlainTextResponse,
    status_code=status.HTTP_200_OK,
)
async def rc_webhook_validation(
    validation_token: str | None = Query(
        default=None,
        alias="validationToken",
        description="RC validation token — echo back to confirm webhook ownership.",
    ),
) -> PlainTextResponse:
    if not validation_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing validationToken query parameter",
        )

    logger.info(
        "RC webhook validation challenge received",
        extra={"event": "rc_validation_challenge", "validation_token": validation_token},
    )
    return PlainTextResponse(content=validation_token, status_code=200)


@router.post(
    "/webhook",
    summary="RC Inbound SMS Webhook Receiver",
    description=(
        "Receives all inbound SMS event notifications from RingCentral. "
        "Validates the Verification-Token header, deduplicates by message ID, "
        "builds the full metadata payload, and forwards to Zapier in near real-time."
    ),
    status_code=status.HTTP_200_OK,
)
async def rc_webhook_receiver(
    request: Request,
    settings: Settings = Depends(get_settings),
    forwarder: ZapierForwarder = Depends(_get_forwarder),
    idempotency: IdempotencyCache = Depends(_get_idempotency_cache),
) -> dict[str, Any]:
    """
    POST /api/v1/rc/webhook

    Full processing pipeline:
      0. Handle Validation-Token challenge (subscription creation/renewal).
      1. Validate Verification-Token header.
      2. Parse raw JSON body.
      3. Parse into RCWebhookEvent schema.
      4. Filter: only process Inbound SMS.
      5. Idempotency check on message ID.
      6. Build ZapierPayload.
      7. Forward to Zapier (with retry).
      8. Mark message ID as seen.
      9. Return 200 OK to RC.
    """

    # ── Step 0: Handle RC Validation Challenge ─────────────────────
    # When creating/renewing a webhook subscription, RC sends a POST
    # with a "Validation-Token" header. We MUST echo it back in the
    # response header with 200 OK. This is separate from the
    # "Verification-Token" used for ongoing push authentication.
    validation_token = request.headers.get("Validation-Token")
    if validation_token:
        logger.info(
            "RC webhook validation challenge received (POST)",
            extra={
                "event": "rc_validation_challenge",
                "validation_token": validation_token,
            },
        )
        return PlainTextResponse(
            content=validation_token,
            status_code=200,
            headers={"Validation-Token": validation_token},
        )

    # ── Step 1: Authenticate ───────────────────────────────────────
    verification_token = request.headers.get("Verification-Token")
    try:
        validate_verification_token(
            received_token=verification_token,
            expected_token=settings.rc_webhook_verification_token,
        )
    except RCValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )

    # ── Step 2: Parse raw body ─────────────────────────────────────
    try:
        raw_body: dict[str, Any] = await request.json()
    except Exception as exc:
        logger.warning(
            "Failed to parse RC webhook JSON body",
            extra={"event": "payload_parse_error", "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    # ── Step 3: Parse into schema ──────────────────────────────────
    try:
        event = RCWebhookEvent.model_validate(raw_body)
    except Exception as exc:
        logger.warning(
            "RC event does not match expected schema",
            extra={"event": "schema_validation_error", "error": str(exc)},
        )
        # Return 200 so RC doesn't retry non-SMS events
        return {"status": "ignored", "reason": "schema_validation_failed"}

    message = event.to_rc_message()

    # ── Step 4: Filter — Inbound SMS only ─────────────────────────
    if message.type and message.type.upper() != "SMS":
        logger.info(
            "Ignoring non-SMS event",
            extra={"event": "event_filtered", "message_type": message.type},
        )
        return {"status": "ignored", "reason": f"message_type={message.type}"}

    if message.direction and message.direction.lower() != "inbound":
        logger.info(
            "Ignoring outbound message",
            extra={"event": "event_filtered", "direction": message.direction},
        )
        return {"status": "ignored", "reason": "outbound"}

    # ── Step 5: Idempotency check ──────────────────────────────────
    message_id = message.id or event.uuid or ""

    if message_id and idempotency.is_duplicate(message_id):
        logger.info(
            "Duplicate message suppressed",
            extra={"event": "duplicate_suppressed", "message_id": message_id},
        )
        return {"status": "duplicate", "message_id": message_id}

    # ── Step 6: Build Zapier payload ───────────────────────────────
    zapier_payload = ZapierPayload.from_rc_event(
        event=event,
        message=message,
        raw_body=raw_body,
    )

    # ── Step 7: Forward to Zapier ──────────────────────────────────
    try:
        result = await forwarder.send(zapier_payload)
    except ZapierForwardError as exc:
        # Return 200 to RC so it doesn't retry (we log the failure internally)
        logger.error(
            "Zapier forward permanently failed — message NOT forwarded",
            extra={
                "event": "zapier_forward_permanent_failure",
                "message_id": message_id,
                "attempts": exc.attempts,
                "last_status_code": exc.last_status_code,
            },
        )
        # Don't mark as seen — allows manual retry
        return {
            "status": "forward_failed",
            "message_id": message_id,
            "detail": "Zapier unreachable after all retries",
        }

    # ── Step 8: Mark message as seen ──────────────────────────────
    if message_id:
        idempotency.mark_seen(message_id)

    # ── Step 9: Return 200 to RC ───────────────────────────────────
    return {
        "status": "forwarded",
        "message_id": result.message_id,
        "attempts": result.attempts,
        "zapier_status_code": result.final_status_code,
    }
