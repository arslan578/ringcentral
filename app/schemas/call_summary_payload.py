"""
app/schemas/call_summary_payload.py

Pydantic model for the call summary POST sent to the Logics endpoint
after a RingCentral call ends and AI notes are fetched.

All fields are flat top-level values (same design as ZapierPayload)
so the Logics/Zapier editor can map each one individually.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


class CallSummaryPayload(BaseModel):
    """
    Flat payload POSTed to the Logics endpoint when a call ends.

    Subject format: "RingCentral Call Summary By: {agent_name}"
    Body: AI-generated call notes text.
    """

    # ── Source identification ──────────────────────────────────────
    source: str = Field(default="ringcentral_call", description="Always 'ringcentral_call'")
    event_type: str = Field(default="call_ended", description="Always 'call_ended'")

    # ── Email-style fields (for Logics / CRM mapping) ─────────────
    subject: str = Field(description="'RingCentral Call Summary By: {agent_name}'")
    body: str = Field(description="AI-generated call notes text")

    # ── Call identity ──────────────────────────────────────────────
    call_id: str = Field(description="RC telephony session ID")
    call_direction: Optional[str] = Field(None, description="'Inbound' or 'Outbound'")

    # ── Parties ────────────────────────────────────────────────────
    agent_name: Optional[str] = Field(None, description="Name of the RC agent/extension")
    agent_number: Optional[str] = Field(None, description="Agent phone number or extension")
    caller_number: Optional[str] = Field(None, description="External caller phone number")
    caller_name: Optional[str] = Field(None, description="External caller name (if available)")

    # ── Call metadata ──────────────────────────────────────────────
    call_duration_seconds: Optional[int] = Field(None, description="Call duration in seconds")
    call_datetime_utc: Optional[str] = Field(None, description="Call start time in ISO-8601 UTC")

    # ── When we processed it ──────────────────────────────────────
    processed_at_utc: str = Field(
        description="When our server processed this event (ISO-8601 UTC)"
    )

    # ── Retry metadata ────────────────────────────────────────────
    notes_retry_attempted: bool = Field(
        default=False,
        description="True if a 30s retry was needed before notes were available",
    )

    @classmethod
    def build(
        cls,
        call_id: str,
        agent_name: Optional[str],
        agent_number: Optional[str],
        caller_number: Optional[str],
        caller_name: Optional[str],
        call_direction: Optional[str],
        call_duration_seconds: Optional[int],
        call_datetime_utc: Optional[str],
        notes: str,
        notes_retry_attempted: bool = False,
    ) -> "CallSummaryPayload":
        """
        Factory: build a CallSummaryPayload from extracted call data.
        """
        agent_label = agent_name or agent_number or "Unknown Agent"
        subject = f"RingCentral Call Summary By: {agent_label}"

        return cls(
            subject=subject,
            body=notes or "(AI notes not available)",
            call_id=call_id,
            call_direction=call_direction,
            agent_name=agent_name,
            agent_number=agent_number,
            caller_number=caller_number,
            caller_name=caller_name,
            call_duration_seconds=call_duration_seconds,
            call_datetime_utc=call_datetime_utc,
            processed_at_utc=datetime.now(timezone.utc).isoformat(),
            notes_retry_attempted=notes_retry_attempted,
        )
