"""
app/api/v1/endpoints/rc_webhook.py

The primary endpoint: receives all SMS webhook pushes from RingCentral.

Two operations on the same path /api/v1/rc/webhook:

  GET  — RC validation challenge (legacy/alternate).
         RC sends a GET with ?validationToken=<token>.
         We MUST echo it back as plain text with 200 OK.

  POST — Two sub-cases:
         a) Validation challenge: RC sends a POST with a "Validation-Token"
            header when creating/renewing a subscription. We echo the token
            back in the response "Validation-Token" header with 200 OK.
         b) Message-store notification: RC posts a CHANGE NOTIFICATION
            containing message IDs (not the actual message content).
            We fetch each message from the RC API, build ZapierPayloads,
            and forward them all.

IMPORTANT:
  RC's message-store webhook does NOT include the SMS body, from/to numbers,
  or any content.  It only includes `changes[].newMessageIds[]`.
  We MUST call the RC REST API to fetch each message by ID.
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
from app.schemas.rc_message import RCMessage, RCWebhookEvent
from app.schemas.zapier_payload import ZapierPayload
from app.services.rc_api_client import RCApiClient
from app.services.zapier_forwarder import ZapierForwarder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rc", tags=["RingCentral Webhook"])

# Width of the separator lines in terminal output
_SEP_WIDTH = 70


def _log_zapier_payload(payload: dict, message_id: str) -> None:
    """
    Print the exact JSON payload being sent to Zapier in a clean,
    readable format directly to the terminal.
    All fields are flat — exactly what Zapier receives.
    """
    direction = payload.get("direction", "Unknown")
    event_type = payload.get("event_type", "sms")

    sep = "-" * _SEP_WIDTH
    header = f" ZAPIER PAYLOAD -- {event_type.upper()} "
    header_line = f"{header:-^{_SEP_WIDTH}}"

    lines = [
        "",
        header_line,
        f"  source              : {payload.get('source')}",
        f"  event_type          : {event_type}",
        f"  message_id          : {payload.get('message_id')}",
        f"  message_type        : {payload.get('message_type')}",
        f"  direction           : {direction}",
        sep,
        f"  from_number         : {payload.get('from_number')}",
        f"  from_name           : {payload.get('from_name')}",
        f"  from_location       : {payload.get('from_location')}",
        sep,
        f"  to_number           : {payload.get('to_number')}",
        f"  to_name             : {payload.get('to_name')}",
        f"  to_location         : {payload.get('to_location')}",
        f"  all_to_numbers      : {payload.get('all_to_numbers')}",
        f"  all_to_names        : {payload.get('all_to_names')}",
        sep,
        f"  subject             : {payload.get('subject', '')!r}",
        f"  body                : {payload.get('body', '')!r}",
        sep,
        f"  timestamp_utc       : {payload.get('timestamp_utc')}",
        f"  last_modified_utc   : {payload.get('last_modified_utc')}",
        f"  sms_delivery_time   : {payload.get('sms_delivery_time_utc')}",
        f"  received_at_utc     : {payload.get('received_at_utc')}",
        sep,
        f"  account_id          : {payload.get('account_id')}",
        f"  extension_id        : {payload.get('extension_id')}",
        f"  subscription_id     : {payload.get('subscription_id')}",
        f"  conversation_id     : {payload.get('conversation_id')}",
        sep,
        f"  read_status         : {payload.get('read_status')}",
        f"  message_status      : {payload.get('message_status')}",
        f"  delivery_error_code : {payload.get('delivery_error_code')}",
        f"  priority            : {payload.get('priority')}",
        f"  availability        : {payload.get('availability')}",
        f"  attachment_count    : {payload.get('attachment_count')}",
        sep,
        f"  message_uri         : {payload.get('message_uri')}",
        f"  rc_event_type       : {payload.get('rc_event_type')}",
        f"  rc_event_uuid       : {payload.get('rc_event_uuid')}",
        sep,
        "",
    ]

    logger.info(
        "Sending to Zapier:\n" + "\n".join(lines),
        extra={
            "event": "zapier_payload_sending",
            "message_id": message_id,
            "direction": direction,
        },
    )


def _get_forwarder(request: Request) -> ZapierForwarder:
    return request.app.state.zapier_forwarder


def _get_idempotency_cache(request: Request) -> IdempotencyCache:
    return request.app.state.idempotency_cache


def _get_rc_api_client(request: Request) -> RCApiClient:
    return request.app.state.rc_api_client


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
    summary="RC SMS Webhook Receiver",
    description=(
        "Receives all SMS event notifications from RingCentral. "
        "Validates the Verification-Token header, fetches full message data "
        "from the RC API, builds the full metadata payload, and forwards "
        "both inbound and outbound SMS to Zapier in near real-time."
    ),
    status_code=status.HTTP_200_OK,
)
async def rc_webhook_receiver(
    request: Request,
    settings: Settings = Depends(get_settings),
    forwarder: ZapierForwarder = Depends(_get_forwarder),
    idempotency: IdempotencyCache = Depends(_get_idempotency_cache),
    rc_api: RCApiClient = Depends(_get_rc_api_client),
) -> dict[str, Any]:
    """
    POST /api/v1/rc/webhook

    Full processing pipeline:
      0. Handle Validation-Token challenge (subscription creation/renewal).
      1. Validate Verification-Token header.
      2. Parse raw JSON body.
      3. Parse into RCWebhookEvent (change notification).
      4. Extract new message IDs from changes[].
      5. Fetch each message from RC REST API (full content).
      6. For each fetched message:
         a. Idempotency check on message ID.
         b. Build ZapierPayload with full message data.
         c. Forward to Zapier (with retry).
         d. Mark message ID as seen.
      7. Return 200 OK to RC.
    """

    # ── Step 0: Handle RC Validation Challenge ─────────────────────
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

    logger.info(
        "RC webhook notification received",
        extra={
            "event": "rc_notification_received",
            "raw_body_keys": list(raw_body.keys()),
        },
    )

    # ── Step 3: Parse into RCWebhookEvent ──────────────────────────
    try:
        event = RCWebhookEvent.model_validate(raw_body)
    except Exception as exc:
        logger.warning(
            "RC event does not match expected schema",
            extra={"event": "schema_validation_error", "error": str(exc)},
        )
        return {"status": "ignored", "reason": "schema_validation_failed"}

    # ── Step 4: Extract new SMS message IDs from notification ──────
    new_message_ids = event.get_new_message_ids()
    account_id = event.get_account_id()
    extension_id = event.get_extension_id()

    if not new_message_ids:
        logger.info(
            "No new SMS message IDs in notification",
            extra={
                "event": "no_sms_messages",
                "body_changes": (
                    [c.model_dump() for c in event.body.changes]
                    if event.body and event.body.changes
                    else []
                ),
            },
        )
        return {"status": "ignored", "reason": "no_new_sms_message_ids"}

    if not account_id or not extension_id:
        logger.error(
            "Cannot determine accountId or extensionId from notification",
            extra={
                "event": "missing_ids",
                "account_id": account_id,
                "extension_id": extension_id,
            },
        )
        return {"status": "error", "reason": "missing_account_or_extension_id"}

    logger.info(
        "Processing SMS notification",
        extra={
            "event": "processing_notification",
            "account_id": account_id,
            "extension_id": extension_id,
            "new_message_ids": new_message_ids,
            "message_count": len(new_message_ids),
        },
    )

    # ── Step 5: Fetch full message data from RC API ────────────────
    fetched_messages = await rc_api.get_messages_batch(
        account_id=account_id,
        extension_id=extension_id,
        message_ids=new_message_ids,
    )

    if not fetched_messages:
        logger.warning(
            "No messages could be fetched from RC API",
            extra={
                "event": "no_messages_fetched",
                "attempted_ids": new_message_ids,
            },
        )
        return {
            "status": "error",
            "reason": "could_not_fetch_messages",
            "attempted_ids": new_message_ids,
        }

    # ── Step 6: Process each fetched message ───────────────────────
    results = []

    for raw_msg in fetched_messages:
        try:
            message = RCMessage.model_validate(raw_msg)
        except Exception as exc:
            logger.warning(
                "Failed to parse fetched RC message",
                extra={
                    "event": "message_parse_error",
                    "error": str(exc),
                    "raw_msg_id": raw_msg.get("id"),
                },
            )
            continue

        message_id = str(message.id) if message.id else ""

        # Filter: only process SMS type
        if message.type and message.type.upper() != "SMS":
            logger.info(
                "Ignoring non-SMS message",
                extra={
                    "event": "event_filtered",
                    "message_type": message.type,
                    "message_id": message_id,
                },
            )
            continue

        # Step 6a: Idempotency check
        if message_id and idempotency.is_duplicate(message_id):
            logger.info(
                "Duplicate message suppressed",
                extra={"event": "duplicate_suppressed", "message_id": message_id},
            )
            results.append({
                "message_id": message_id,
                "status": "duplicate",
                "direction": message.direction,
            })
            continue

        # Step 6b: Build Zapier payload from full message data
        zapier_payload = ZapierPayload.from_rc_message(
            message=message,
            raw_message=raw_msg,
            account_id=account_id,
            extension_id=extension_id,
            subscription_id=event.subscription_id,
            rc_event_type=event.event,
            rc_event_uuid=event.uuid,
        )

        # Print the exact Zapier payload to terminal
        payload_dict = zapier_payload.model_dump(mode="json")
        _log_zapier_payload(payload_dict, message_id)

        # Step 6c: Route to the correct Zapier URL based on direction
        #   Inbound  → ZAPIER_INBOUND_WEBHOOK_URL
        #   Outbound → ZAPIER_OUTBOUND_WEBHOOK_URL
        is_inbound = (message.direction or "").lower() == "inbound"
        target_zapier_url = (
            settings.zapier_inbound_webhook_url
            if is_inbound
            else settings.zapier_outbound_webhook_url
        )

        try:
            result = await forwarder.send(zapier_payload, webhook_url=target_zapier_url)

            # Step 6d: Mark message as seen
            if message_id:
                idempotency.mark_seen(message_id)

            results.append({
                "message_id": result.message_id,
                "status": "forwarded",
                "direction": message.direction,
                "attempts": result.attempts,
                "zapier_status_code": result.final_status_code,
            })
        except ZapierForwardError as exc:
            logger.error(
                "Zapier forward permanently failed",
                extra={
                    "event": "zapier_forward_permanent_failure",
                    "message_id": message_id,
                    "direction": message.direction,
                    "attempts": exc.attempts,
                    "last_status_code": exc.last_status_code,
                },
            )
            results.append({
                "message_id": message_id,
                "status": "forward_failed",
                "direction": message.direction,
                "detail": "Zapier unreachable after all retries",
            })

    # ── Step 7: Return 200 to RC ───────────────────────────────────
    return {
        "status": "processed",
        "total_message_ids": len(new_message_ids),
        "fetched": len(fetched_messages),
        "results": results,
    }
