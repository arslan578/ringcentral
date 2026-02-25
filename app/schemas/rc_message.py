"""
app/schemas/rc_message.py

Pydantic models representing the RingCentral inbound SMS event payload.

RC wraps the SMS message object inside a notification body. We model
both the outer envelope and the inner message object to capture 100%
of available metadata, as required by the SOW.

Reference:
  https://developers.ringcentral.com/api-reference/SMS-and-MMS
  https://developers.ringcentral.com/api-reference/Message-Store
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ─────────────────────────────────────────────────────────────────
# Inner models: RC Message sub-objects
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
    Full RC SMS message object.
    extra="allow" ensures any undocumented or future RC fields are preserved.
    """

    model_config = {"extra": "allow", "populate_by_name": True}

    # Core identity
    id: Optional[str] = Field(None, description="Unique RC message ID")
    uri: Optional[str] = None
    type: Optional[str] = Field(None, description="e.g. 'SMS'")
    direction: Optional[str] = Field(None, description="'Inbound' | 'Outbound'")

    # Parties
    from_: Optional[RCPhoneNumber] = Field(None, alias="from")
    to: Optional[list[RCPhoneNumber]] = None

    # Content
    subject: Optional[str] = Field(None, description="SMS message body text")
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
    conversation_id: Optional[str] = Field(None, alias="conversationId")

    # RC hierarchy
    pg_to_dept_name: Optional[str] = Field(None, alias="pgToDeptName")
    pg_to_dept_id: Optional[str] = Field(None, alias="pgToDeptId")

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


# ─────────────────────────────────────────────────────────────────
# Outer RC Notification Envelope
# ─────────────────────────────────────────────────────────────────

class RCNotificationBody(BaseModel):
    """The `body` field inside the RC webhook notification."""

    model_config = {"extra": "allow"}

    id: Optional[str] = None
    uri: Optional[str] = None
    event_time: Optional[datetime] = Field(None, alias="eventTime")

    # The actual message object lives here
    # RC can send it as a top-level structure or nested
    type: Optional[str] = None
    direction: Optional[str] = None
    from_: Optional[RCPhoneNumber] = Field(None, alias="from")
    to: Optional[list[RCPhoneNumber]] = None
    subject: Optional[str] = None
    attachments: Optional[list[RCAttachment]] = None
    creation_time: Optional[datetime] = Field(None, alias="creationTime")
    last_modified_time: Optional[datetime] = Field(None, alias="lastModifiedTime")
    read_status: Optional[str] = Field(None, alias="readStatus")
    priority: Optional[str] = None
    availability: Optional[str] = None
    message_status: Optional[str] = Field(None, alias="messageStatus")
    delivery_error_code: Optional[str] = Field(None, alias="deliveryErrorCode")
    conversation: Optional[RCConversation] = None
    conversation_id: Optional[str] = Field(None, alias="conversationId")


class RCWebhookEvent(BaseModel):
    """
    Top-level RingCentral webhook notification envelope.
    RC sends this JSON body to our endpoint on every subscribed event.
    """

    model_config = {"extra": "allow", "populate_by_name": True}

    uuid: Optional[str] = None
    event: Optional[str] = None                    # e.g. "/restapi/v1.0/account/.../extension/.../message-store"
    timestamp: Optional[datetime] = None
    subscription_id: Optional[str] = Field(None, alias="subscriptionId")
    body: Optional[RCNotificationBody] = None

    # Sometimes RC embeds account/extension context at the top level
    account_id: Optional[str] = Field(None, alias="ownerId")
    extension_id: Optional[str] = None

    def to_rc_message(self) -> RCMessage:
        """
        Extract the SMS message from the notification body.
        Returns an RCMessage populated from whatever fields are available.
        """
        body_data = self.body.model_dump(by_alias=False) if self.body else {}
        return RCMessage(**body_data)
