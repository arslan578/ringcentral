"""
app/schemas/zapier_payload.py

Defines the exact JSON structure sent to the Zapier webhook.

Design goals:
  - All SOW-required fields are explicitly named and documented.
  - raw_rc_message preserves the complete original RC message so Zapier/Zap
    downstream logic can access any field we didn't explicitly map.
  - Serializes datetimes as ISO-8601 UTC strings.
  - Handles BOTH inbound and outbound SMS messages.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.schemas.rc_message import RCMessage


class ZapierPayload(BaseModel):
    """
    The full metadata payload POSTed to Zapier.
    Every field maps to a Zapier input in your Zap editor.
    """

    # ── Source identification ──────────────────────────────────────
    source: str = Field(default="ringcentral", description="Always 'ringcentral'")
    event_type: str = Field(description="'inbound_sms' or 'outbound_sms'")

    # ── Message identity ───────────────────────────────────────────
    message_id: str = Field(description="RC unique message ID")
    direction: str = Field(description="'Inbound' or 'Outbound'")

    # ── Party phone numbers ────────────────────────────────────────
    from_number: str = Field(description="Sender E.164 phone number")
    to_number: str = Field(description="Receiving E.164 phone number")

    # ── Content ───────────────────────────────────────────────────
    body: str = Field(description="SMS message body text")

    # ── Timestamps ────────────────────────────────────────────────
    timestamp_utc: str = Field(description="Message creation time in ISO-8601 UTC")
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

    # ── RC event envelope metadata ────────────────────────────────
    rc_event_type: Optional[str] = Field(None, description="RC event URI path")
    rc_event_uuid: Optional[str] = Field(None, description="RC notification UUID")

    # ── Full raw message (SOW: transmit all available metadata) ───
    raw_rc_payload: dict[str, Any] = Field(
        description="Complete original RC message object for full metadata access"
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

        Args:
            message:        The parsed RCMessage (from RC REST API).
            raw_message:    The original, unparsed message JSON dict.
            account_id:     RC account ID from the notification.
            extension_id:   RC extension ID from the notification.
            subscription_id: RC webhook subscription ID.
            rc_event_type:  RC event URI path.
            rc_event_uuid:  RC notification UUID.
        """
        now_utc = datetime.now(timezone.utc).isoformat()

        # Determine message creation timestamp
        if message.creation_time:
            ts = message.creation_time
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            timestamp_utc = ts.isoformat()
        else:
            timestamp_utc = now_utc

        # Determine direction and event_type
        direction = message.direction or "Unknown"
        if direction.lower() == "inbound":
            event_type = "inbound_sms"
        elif direction.lower() == "outbound":
            event_type = "outbound_sms"
        else:
            event_type = "sms"

        # Conversation ID
        conv_id = None
        if message.conversation_id:
            conv_id = str(message.conversation_id)
        elif message.conversation and message.conversation.id:
            conv_id = str(message.conversation.id)

        return cls(
            event_type=event_type,
            message_id=str(message.id) if message.id else "unknown",
            direction=direction,
            from_number=message.from_number,
            to_number=message.to_number,
            body=message.body,
            timestamp_utc=timestamp_utc,
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
            rc_event_type=rc_event_type,
            rc_event_uuid=rc_event_uuid,
            raw_rc_payload=raw_message,
        )
