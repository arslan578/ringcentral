"""
app/schemas/zapier_payload.py

Defines the exact JSON structure sent to the Zapier webhook.

Design goals:
  - All SOW-required fields are explicitly named and documented.
  - raw_rc_payload preserves the complete original RC body so Zapier/Zap
    downstream logic can access any field we didn't explicitly map.
  - Serializes datetimes as ISO-8601 UTC strings.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, model_serializer

from app.schemas.rc_message import RCMessage, RCWebhookEvent


class ZapierPayload(BaseModel):
    """
    The full metadata payload POSTed to Zapier.
    Every field maps to a Zapier input in your Zap editor.
    """

    # ── Source identification ──────────────────────────────────────
    source: str = Field(default="ringcentral", description="Always 'ringcentral'")
    event_type: str = Field(default="inbound_sms", description="Always 'inbound_sms'")

    # ── Message identity ───────────────────────────────────────────
    message_id: str = Field(description="RC unique message ID")
    direction: str = Field(description="Always 'Inbound' per SOW filter")

    # ── Party phone numbers ────────────────────────────────────────
    from_number: str = Field(description="Sender E.164 phone number")
    to_number: str = Field(description="Receiving RC user E.164 phone number")

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
    message_status: Optional[str] = Field(None, description="e.g. 'Received'")
    delivery_error_code: Optional[str] = Field(None, description="Carrier error code if any")
    priority: Optional[str] = Field(None, description="e.g. 'Normal'")
    availability: Optional[str] = Field(None, description="e.g. 'Alive'")

    # ── Attachment info ───────────────────────────────────────────
    attachment_count: int = Field(default=0, description="Number of attachments (MMS)")

    # ── RC event envelope metadata ────────────────────────────────
    rc_event_type: Optional[str] = Field(None, description="RC event URI path")
    rc_event_uuid: Optional[str] = Field(None, description="RC notification UUID")

    # ── Full raw payload (SOW: transmit all available metadata) ───
    raw_rc_payload: dict[str, Any] = Field(
        description="Complete original RC notification body for full metadata access"
    )

    @classmethod
    def from_rc_event(
        cls,
        event: RCWebhookEvent,
        message: RCMessage,
        raw_body: dict[str, Any],
    ) -> "ZapierPayload":
        """
        Factory: build a ZapierPayload from a parsed RC event + message.

        Args:
            event:    The parsed top-level RC notification envelope.
            message:  The extracted RCMessage from the body.
            raw_body: The original, unparsed request JSON dict.
        """
        now_utc = datetime.now(timezone.utc).isoformat()

        # Determine message creation timestamp
        if message.creation_time:
            ts = message.creation_time
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            timestamp_utc = ts.isoformat()
        elif event.timestamp:
            ts = event.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            timestamp_utc = ts.isoformat()
        else:
            timestamp_utc = now_utc

        return cls(
            message_id=message.id or event.uuid or "unknown",
            direction=message.direction or "Inbound",
            from_number=message.from_number,
            to_number=message.to_number,
            body=message.body,
            timestamp_utc=timestamp_utc,
            received_at_utc=now_utc,
            account_id=event.account_id,
            extension_id=event.extension_id,
            subscription_id=event.subscription_id,
            conversation_id=(
                message.conversation_id
                or (message.conversation.id if message.conversation else None)
            ),
            read_status=message.read_status,
            message_status=message.message_status,
            delivery_error_code=message.delivery_error_code,
            priority=message.priority,
            availability=message.availability,
            attachment_count=len(message.attachments) if message.attachments else 0,
            rc_event_type=event.event,
            rc_event_uuid=event.uuid,
            raw_rc_payload=raw_body,
        )
