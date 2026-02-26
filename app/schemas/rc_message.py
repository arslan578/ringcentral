"""
app/schemas/rc_message.py

Pydantic models representing the RingCentral webhook notification payload
and the full SMS message object fetched from the RC Message Store API.

RC's message-store webhook sends a CHANGE NOTIFICATION — not the full
message.  The notification body contains:
  - accountId, extensionId, lastUpdated
  - changes[]: { type, newCount, updatedCount, newMessageIds[] }

We must then call the RC REST API to fetch each message by ID to get
the actual SMS content (from, to, subject/body, direction, etc.).

Reference:
  https://developers.ringcentral.com/api-reference/SMS-and-MMS
  https://developers.ringcentral.com/api-reference/Message-Store
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────
# RC Notification models (what the webhook actually delivers)
# ─────────────────────────────────────────────────────────────────

class RCChangeRecord(BaseModel):
    """One entry in the `changes` array of an RC notification body."""
    model_config = {"extra": "allow"}

    type: Optional[str] = None                    # e.g. "SMS"
    new_count: int = Field(default=0, alias="newCount")
    updated_count: int = Field(default=0, alias="updatedCount")
    new_message_ids: list[int | str] = Field(default_factory=list, alias="newMessageIds")


class RCNotificationBody(BaseModel):
    """
    The `body` field inside the RC webhook notification.
    This is a CHANGE NOTIFICATION, NOT the actual message data.
    """
    model_config = {"extra": "allow", "populate_by_name": True}

    account_id: Optional[int | str] = Field(None, alias="accountId")
    extension_id: Optional[int | str] = Field(None, alias="extensionId")
    last_updated: Optional[str] = Field(None, alias="lastUpdated")
    changes: list[RCChangeRecord] = Field(default_factory=list)


class RCWebhookEvent(BaseModel):
    """
    Top-level RingCentral webhook notification envelope.
    RC sends this JSON body to our endpoint on every subscribed event.
    """
    model_config = {"extra": "allow", "populate_by_name": True}

    uuid: Optional[str] = None
    event: Optional[str] = None           # e.g. "/restapi/v1.0/account/.../extension/.../message-store"
    timestamp: Optional[datetime] = None
    subscription_id: Optional[str] = Field(None, alias="subscriptionId")
    owner_id: Optional[str] = Field(None, alias="ownerId")
    body: Optional[RCNotificationBody] = None

    def get_account_id(self) -> Optional[str]:
        """Extract account ID from notification body or event URI."""
        if self.body and self.body.account_id:
            return str(self.body.account_id)
        if self.owner_id:
            return self.owner_id
        # Try to parse from event URI: /restapi/v1.0/account/{id}/...
        if self.event:
            parts = self.event.split("/")
            try:
                idx = parts.index("account")
                return parts[idx + 1]
            except (ValueError, IndexError):
                pass
        return None

    def get_extension_id(self) -> Optional[str]:
        """Extract extension ID from notification body or event URI."""
        if self.body and self.body.extension_id:
            return str(self.body.extension_id)
        # Try to parse from event URI: .../extension/{id}/...
        if self.event:
            parts = self.event.split("/")
            try:
                idx = parts.index("extension")
                return parts[idx + 1]
            except (ValueError, IndexError):
                pass
        return None

    def get_new_message_ids(self) -> list[str]:
        """
        Collect all new message IDs from all SMS change records.
        Returns IDs as strings.
        """
        ids: list[str] = []
        if self.body and self.body.changes:
            for change in self.body.changes:
                if change.type and change.type.upper() == "SMS":
                    ids.extend(str(mid) for mid in change.new_message_ids)
        return ids


# ─────────────────────────────────────────────────────────────────
# Full RC Message models (fetched from RC REST API by message ID)
# ─────────────────────────────────────────────────────────────────

class RCPhoneNumber(BaseModel):
    """Represents a from/to phone number inside an RC message."""
    model_config = {"extra": "allow"}

    phone_number: Optional[str] = Field(None, alias="phoneNumber")
    name: Optional[str] = None
    location: Optional[str] = None
    message_status: Optional[str] = Field(None, alias="messageStatus")


class RCAttachment(BaseModel):
    """Represents a message attachment (e.g. MMS)."""
    model_config = {"extra": "allow"}

    id: Optional[str] = None
    uri: Optional[str] = None
    type: Optional[str] = None
    content_type: Optional[str] = Field(None, alias="contentType")


class RCConversation(BaseModel):
    model_config = {"extra": "allow"}

    id: Optional[str] = None
    uri: Optional[str] = None


class RCMessage(BaseModel):
    """
    Full RC SMS message object — fetched from the Message Store API.
    extra="allow" ensures any undocumented or future RC fields are preserved.
    """
    model_config = {"extra": "allow", "populate_by_name": True}

    # Core identity
    id: Optional[str | int] = Field(None, description="Unique RC message ID")
    uri: Optional[str] = None
    type: Optional[str] = Field(None, description="e.g. 'SMS'")
    direction: Optional[str] = Field(None, description="'Inbound' | 'Outbound'")

    # Parties
    from_: Optional[RCPhoneNumber] = Field(None, alias="from")
    to: Optional[list[RCPhoneNumber]] = None

    # Content
    subject: Optional[str] = Field(None, description="SMS message body text")

    # Attachments
    attachments: Optional[list[RCAttachment]] = None

    # Timestamps
    creation_time: Optional[datetime] = Field(None, alias="creationTime")
    last_modified_time: Optional[datetime] = Field(None, alias="lastModifiedTime")

    # Status flags
    read_status: Optional[str] = Field(None, alias="readStatus")
    priority: Optional[str] = None
    availability: Optional[str] = None
    message_status: Optional[str] = Field(None, alias="messageStatus")
    delivery_error_code: Optional[str] = Field(None, alias="deliveryErrorCode")

    # Threading
    conversation: Optional[RCConversation] = None
    conversation_id: Optional[str | int] = Field(None, alias="conversationId")

    # SMS-specific
    sms_delivery_time: Optional[datetime] = Field(None, alias="smsDeliveryTime")
    smil_xml: Optional[str] = Field(None, alias="smilXml")

    @property
    def from_number(self) -> str:
        """Convenience: sender E.164 phone number."""
        if self.from_ and self.from_.phone_number:
            return self.from_.phone_number
        return ""

    @property
    def to_number(self) -> str:
        """Convenience: first recipient E.164 phone number."""
        if self.to and self.to[0].phone_number:
            return self.to[0].phone_number
        return ""

    @property
    def body(self) -> str:
        """Message body text (subject field in RC API)."""
        return self.subject or ""
