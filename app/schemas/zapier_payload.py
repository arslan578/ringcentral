"""
app/schemas/zapier_payload.py

Defines the exact JSON structure sent to the Zapier webhook.

Design goals:
  - ALL fields are flat, top-level strings/numbers so Zapier can display
    and map each one individually in the Zap editor.
  - No nested objects — Zapier cannot easily parse nested JSON.
  - raw_rc_payload is a JSON *string* (not a dict) for reference only.
  - Handles BOTH inbound and outbound SMS messages.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.schemas.rc_message import RCMessage


class ZapierPayload(BaseModel):
    """
    The full metadata payload POSTed to Zapier.
    Every field is a flat, top-level value so Zapier shows each one
    as a separate mappable field in the Zap editor.
    """

    # ── Source identification ──────────────────────────────────────
    source: str = Field(default="ringcentral", description="Always 'ringcentral'")
    event_type: str = Field(description="'inbound_sms' or 'outbound_sms'")

    # ── Message identity ───────────────────────────────────────────
    message_id: str = Field(description="RC unique message ID")
    message_type: str = Field(default="SMS", description="Message type, e.g. 'SMS'")
    direction: str = Field(description="'Inbound' or 'Outbound'")

    # ── Sender info (flat) ─────────────────────────────────────────
    from_number: str = Field(description="Sender E.164 phone number")
    from_name: Optional[str] = Field(None, description="Sender name (if available)")
    from_location: Optional[str] = Field(None, description="Sender location (if available)")

    # ── Recipient info (flat) ──────────────────────────────────────
    to_number: str = Field(description="Receiving E.164 phone number")
    to_name: Optional[str] = Field(None, description="Recipient name (if available)")
    to_location: Optional[str] = Field(None, description="Recipient location (if available)")

    # ── Content ───────────────────────────────────────────────────
    subject: str = Field(default="", description="SMS subject (same as body for SMS)")
    body: str = Field(description="SMS message body text")

    # ── Timestamps ────────────────────────────────────────────────
    timestamp_utc: str = Field(description="Message creation time in ISO-8601 UTC")
    last_modified_utc: Optional[str] = Field(None, description="Last modified time in ISO-8601 UTC")
    sms_delivery_time_utc: Optional[str] = Field(None, description="SMS delivery time in ISO-8601 UTC")
    received_at_utc: str = Field(
        description="When our server received and processed this event (ISO-8601 UTC)"
    )

    # ── RC hierarchy ──────────────────────────────────────────────
    account_id: Optional[str] = Field(None, description="RC account/owner ID")
    extension_id: Optional[str] = Field(None, description="RC extension ID")
    subscription_id: Optional[str] = Field(None, description="RC webhook subscription ID")

    # ── Threading ─────────────────────────────────────────────────
    conversation_id: Optional[str] = Field(None, description="RC conversation/thread ID")

    # ── Status flags ──────────────────────────────────────────────
    read_status: Optional[str] = Field(None, description="e.g. 'Unread'")
    message_status: Optional[str] = Field(None, description="e.g. 'Received' or 'Sent'")
    delivery_error_code: Optional[str] = Field(None, description="Carrier error code if any")
    priority: Optional[str] = Field(None, description="e.g. 'Normal'")
    availability: Optional[str] = Field(None, description="e.g. 'Alive'")

    # ── Attachment info ───────────────────────────────────────────
    attachment_count: int = Field(default=0, description="Number of attachments (MMS)")

    # ── RC resource URI ───────────────────────────────────────────
    message_uri: Optional[str] = Field(None, description="RC API URI for this message")

    # ── RC event envelope metadata ────────────────────────────────
    rc_event_type: Optional[str] = Field(None, description="RC event URI path")
    rc_event_uuid: Optional[str] = Field(None, description="RC notification UUID")

    # ── Full raw message as a flat JSON string (for reference) ────
    raw_rc_payload: str = Field(
        default="{}",
        description="Complete original RC message as a JSON string"
    )

    @classmethod
    def from_rc_message(
        cls,
        message: RCMessage,
        raw_message: dict[str, Any],
        account_id: Optional[str] = None,
        extension_id: Optional[str] = None,
        subscription_id: Optional[str] = None,
        rc_event_type: Optional[str] = None,
        rc_event_uuid: Optional[str] = None,
    ) -> "ZapierPayload":
        """
        Factory: build a ZapierPayload from a full RC message (fetched from API).
        All nested data is flattened into top-level fields.
        """
        now_utc = datetime.now(timezone.utc).isoformat()

        # -- Timestamps --
        if message.creation_time:
            ts = message.creation_time
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            timestamp_utc = ts.isoformat()
        else:
            timestamp_utc = now_utc

        last_modified_utc = None
        if message.last_modified_time:
            lm = message.last_modified_time
            if lm.tzinfo is None:
                lm = lm.replace(tzinfo=timezone.utc)
            last_modified_utc = lm.isoformat()

        sms_delivery_time_utc = None
        if message.sms_delivery_time:
            sd = message.sms_delivery_time
            if sd.tzinfo is None:
                sd = sd.replace(tzinfo=timezone.utc)
            sms_delivery_time_utc = sd.isoformat()

        # -- Direction and event type --
        direction = message.direction or "Unknown"
        if direction.lower() == "inbound":
            event_type = "inbound_sms"
        elif direction.lower() == "outbound":
            event_type = "outbound_sms"
        else:
            event_type = "sms"

        # -- Sender info (flat) --
        from_name = None
        from_location = None
        if message.from_:
            from_name = message.from_.name
            from_location = message.from_.location

        # -- Recipient info (flat) --
        to_name = None
        to_location = None
        if message.to and len(message.to) > 0:
            to_name = message.to[0].name
            to_location = message.to[0].location

        # -- Conversation ID --
        conv_id = None
        if message.conversation_id:
            conv_id = str(message.conversation_id)
        elif message.conversation and message.conversation.id:
            conv_id = str(message.conversation.id)

        # -- Raw payload as a flat JSON string --
        raw_json_str = json.dumps(raw_message, default=str)

        return cls(
            event_type=event_type,
            message_id=str(message.id) if message.id else "unknown",
            message_type=message.type or "SMS",
            direction=direction,
            from_number=message.from_number,
            from_name=from_name,
            from_location=from_location,
            to_number=message.to_number,
            to_name=to_name,
            to_location=to_location,
            subject=message.subject or "",
            body=message.body,
            timestamp_utc=timestamp_utc,
            last_modified_utc=last_modified_utc,
            sms_delivery_time_utc=sms_delivery_time_utc,
            received_at_utc=now_utc,
            account_id=account_id,
            extension_id=extension_id,
            subscription_id=subscription_id,
            conversation_id=conv_id,
            read_status=message.read_status,
            message_status=message.message_status,
            delivery_error_code=message.delivery_error_code,
            priority=message.priority,
            availability=message.availability,
            attachment_count=len(message.attachments) if message.attachments else 0,
            message_uri=message.uri,
            rc_event_type=rc_event_type,
            rc_event_uuid=rc_event_uuid,
            raw_rc_payload=raw_json_str,
        )
