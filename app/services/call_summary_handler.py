"""
app/services/call_summary_handler.py

Handles the RingCentral telephony call-ended event.

Flow:
  1. Receive the raw call-ended webhook payload (already parsed as dict).
  2. Extract call_id (telephonySessionId) and the agent identity.
  3. Call the RC Call Log API to fetch AI notes for that call.
  4. If notes are not yet ready (AI takes time), wait 30 seconds and retry once.
  5. Build a CallSummaryPayload and POST it to the Logics endpoint.
  6. Always return (never raise) — log errors and carry on.

This handler runs ALONGSIDE the existing SMS webhook logic, not instead of it.
A call-ended event has a different event path from an SMS notification, so the
two code paths never overlap.

RC Call-Ended Event Path:
  /restapi/v1.0/account/{accountId}/telephony/sessions

RC Call Log API:
  GET /restapi/v1.0/account/{accountId}/call-log/{sessionId}?view=Detailed

Notes field location in call log:
  call_log.notes  (string, may be absent if AI hasn't finished processing)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from app.schemas.call_summary_payload import CallSummaryPayload
from app.services.rc_api_client import RCApiClient

logger = logging.getLogger(__name__)

# How long to wait before retrying if AI notes aren't ready yet
_NOTES_RETRY_DELAY_SECONDS = 30


def _extract_call_info(raw_body: dict[str, Any]) -> tuple[str, str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[int], Optional[str]]:
    """
    Extract essential call fields from the raw telephony session webhook payload.

    RC telephony session payloads look like:
    {
      "uuid": "...",
      "event": "/restapi/v1.0/account/{accountId}/telephony/sessions",
      "body": {
        "accountId": "...",
        "telephonySessionId": "sessionId_abc",
        "sessionId": "sessionId_abc",
        "parties": [
          {
            "accountId": "...",
            "extensionId": "...",
            "id": "party-id",
            "direction": "Inbound" | "Outbound",
            "status": {"code": "Disconnected", ...},
            "from": {"phoneNumber": "+1...", "name": "..."},
            "to": {"phoneNumber": "+1...", "name": "..."},
            "muted": false
          }
        ]
      }
    }

    Returns:
        (account_id, call_id, agent_name, agent_number,
         caller_number, caller_name, call_direction, duration, start_time)
    """
    body = raw_body.get("body", {}) or {}

    account_id = str(
        body.get("accountId")
        or raw_body.get("ownerId")
        or "~"
    )

    # RC uses different field names for the session ID across API versions
    call_id = str(
        body.get("telephonySessionId")
        or body.get("sessionId")
        or ""
    )

    # Parse parties to find the agent (internal RC extension) and external caller
    parties: list[dict] = body.get("parties", []) or []

    agent_name: Optional[str] = None
    agent_number: Optional[str] = None
    caller_number: Optional[str] = None
    caller_name: Optional[str] = None
    call_direction: Optional[str] = None

    for party in parties:
        direction = party.get("direction", "")
        from_info = party.get("from") or {}
        to_info = party.get("to") or {}

        # The RC agent is the party with an extensionId
        if party.get("extensionId"):
            # This is the internal RC user (agent)
            call_direction = direction

            if direction == "Inbound":
                # Agent is the "to" side; caller is "from"
                agent_name = to_info.get("name") or party.get("name")
                agent_number = (
                    to_info.get("phoneNumber")
                    or to_info.get("extensionNumber")
                    or str(party.get("extensionId", ""))
                )
                caller_number = from_info.get("phoneNumber") or from_info.get("extensionNumber")
                caller_name = from_info.get("name")
            else:
                # Outbound — agent is the "from" side
                agent_name = from_info.get("name") or party.get("name")
                agent_number = (
                    from_info.get("phoneNumber")
                    or from_info.get("extensionNumber")
                    or str(party.get("extensionId", ""))
                )
                caller_number = to_info.get("phoneNumber") or to_info.get("extensionNumber")
                caller_name = to_info.get("name")
            break

    # Fallback: if no extensionId found, use first party
    if not agent_name and parties:
        first = parties[0]
        from_info = first.get("from") or {}
        to_info = first.get("to") or {}
        agent_name = from_info.get("name") or to_info.get("name")
        agent_number = from_info.get("phoneNumber") or from_info.get("extensionNumber")
        call_direction = first.get("direction")

    return (
        account_id,
        call_id,
        agent_name,
        agent_number,
        caller_number,
        caller_name,
        call_direction,
        None,    # duration (fetched from call log)
        None,    # start_time (fetched from call log)
    )


def _extract_notes_from_call_log(call_log: dict[str, Any]) -> str:
    """
    Extract AI notes text from a call log entry.

    RC may put notes in different places depending on account features:
      1. call_log['notes']     - direct notes field (most common)
      2. call_log['aiNotes']   - explicit AI notes field
      3. call_log['transcription']['text'] - transcription text as fallback

    Returns empty string if none found (handler will retry or use fallback).
    """
    if not call_log:
        return ""

    # Primary: direct notes field
    notes = call_log.get("notes") or ""
    if notes and notes.strip():
        return notes.strip()

    # Secondary: explicit AI notes field
    ai_notes = call_log.get("aiNotes") or ""
    if ai_notes and ai_notes.strip():
        return ai_notes.strip()

    # Tertiary: transcription text
    transcription = call_log.get("transcription") or {}
    if isinstance(transcription, dict):
        trans_text = transcription.get("text") or ""
        if trans_text and trans_text.strip():
            return trans_text.strip()

    return ""


class CallSummaryHandler:
    """
    Handles RingCentral telephony call-ended events end-to-end.

    Instantiated once at startup and stored on app.state.call_summary_handler.
    """

    def __init__(
        self,
        rc_api: RCApiClient,
        http_client: httpx.AsyncClient,
        logics_url: str,
        notes_retry_delay: float = _NOTES_RETRY_DELAY_SECONDS,
    ) -> None:
        self._rc_api = rc_api
        self._http = http_client
        self._logics_url = logics_url
        self._notes_retry_delay = notes_retry_delay

    async def handle(self, raw_body: dict[str, Any]) -> dict[str, Any]:
        """
        Process a call-ended webhook event.

        Steps:
          1. Extract call_id and agent info from the payload.
          2. Fetch the call log entry (which may contain AI notes).
          3. If notes are empty, wait and retry once after a delay.
          4. Build CallSummaryPayload and POST to Logics endpoint.
          5. Return a status dict (never raises — errors are logged).

        Args:
            raw_body: The full decoded webhook request body dict.

        Returns:
            dict with 'status' and contextual fields.
        """
        (
            account_id,
            call_id,
            agent_name,
            agent_number,
            caller_number,
            caller_name,
            call_direction,
            _,
            __,
        ) = _extract_call_info(raw_body)

        if not call_id:
            logger.warning(
                "Call-ended event missing session/call ID — cannot fetch notes",
                extra={"event": "call_summary_no_call_id", "raw_keys": list(raw_body.keys())},
            )
            return {"status": "skipped", "reason": "no_call_id"}

        logger.info(
            "Processing call-ended event",
            extra={
                "event": "call_summary_start",
                "call_id": call_id,
                "account_id": account_id,
                "agent_name": agent_name,
                "call_direction": call_direction,
            },
        )

        # ── Step 2: Fetch call log with AI notes ─────────────────
        call_log = await self._rc_api.get_call_log_entry(
            account_id=account_id,
            call_id=call_id,
        )

        notes = _extract_notes_from_call_log(call_log or {})
        retry_attempted = False

        # ── Step 3: Retry once if notes not ready ────────────────
        if not notes:
            logger.info(
                "AI notes not yet ready — waiting %ss before retry",
                self._notes_retry_delay,
                extra={
                    "event": "call_summary_notes_not_ready",
                    "call_id": call_id,
                    "retry_delay": self._notes_retry_delay,
                },
            )
            await asyncio.sleep(self._notes_retry_delay)
            retry_attempted = True

            call_log = await self._rc_api.get_call_log_entry(
                account_id=account_id,
                call_id=call_id,
            )
            notes = _extract_notes_from_call_log(call_log or {})

            if not notes:
                logger.warning(
                    "AI notes still not available after retry — using fallback",
                    extra={"event": "call_summary_notes_unavailable", "call_id": call_id},
                )

        # ── Step 4: Extract remaining metadata from call log ─────
        duration: Optional[int] = None
        call_datetime_utc: Optional[str] = None

        if call_log:
            raw_duration = call_log.get("duration")
            if raw_duration is not None:
                try:
                    duration = int(raw_duration)
                except (ValueError, TypeError):
                    pass

            start_time = call_log.get("startTime") or ""
            if start_time:
                call_datetime_utc = start_time

            # Refine agent/caller from call log if not available from event
            if not agent_name or not caller_number:
                log_parties = call_log.get("legs") or []
                for leg in log_parties:
                    leg_from = leg.get("from") or {}
                    leg_to = leg.get("to") or {}
                    if not agent_name:
                        agent_name = leg_from.get("name") or leg_to.get("name")
                    if not caller_number:
                        caller_number = leg_from.get("phoneNumber") or leg_to.get("phoneNumber")
                    if not call_direction:
                        call_direction = leg.get("direction")

        # ── Step 5: Build payload ────────────────────────────────
        payload = CallSummaryPayload.build(
            call_id=call_id,
            agent_name=agent_name,
            agent_number=agent_number,
            caller_number=caller_number,
            caller_name=caller_name,
            call_direction=call_direction,
            call_duration_seconds=duration,
            call_datetime_utc=call_datetime_utc,
            notes=notes,
            notes_retry_attempted=retry_attempted,
        )

        self._log_payload(payload)

        # ── Step 6: POST to Logics endpoint ──────────────────────
        if not self._logics_url:
            logger.warning(
                "LOGICS_WEBHOOK_URL not configured — call summary not sent",
                extra={"event": "call_summary_no_logics_url", "call_id": call_id},
            )
            return {"status": "skipped", "reason": "no_logics_url", "call_id": call_id}

        return await self._post_to_logics(payload)

    async def _post_to_logics(self, payload: CallSummaryPayload) -> dict[str, Any]:
        """POST the call summary payload to the Logics endpoint."""
        try:
            response = await self._http.post(
                self._logics_url,
                json=payload.model_dump(mode="json"),
                timeout=15.0,
            )

            if response.is_success:
                logger.info(
                    "Call summary POSTed to Logics successfully",
                    extra={
                        "event": "call_summary_sent",
                        "call_id": payload.call_id,
                        "logics_status_code": response.status_code,
                        "notes_retry_attempted": payload.notes_retry_attempted,
                    },
                )
                return {
                    "status": "sent",
                    "call_id": payload.call_id,
                    "logics_status_code": response.status_code,
                    "notes_retry_attempted": payload.notes_retry_attempted,
                }
            else:
                logger.error(
                    "Logics endpoint returned non-success status",
                    extra={
                        "event": "call_summary_logics_error",
                        "call_id": payload.call_id,
                        "status_code": response.status_code,
                        "response_body": response.text[:300],
                    },
                )
                return {
                    "status": "logics_error",
                    "call_id": payload.call_id,
                    "logics_status_code": response.status_code,
                }

        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error(
                "Failed to POST call summary to Logics endpoint",
                extra={
                    "event": "call_summary_post_error",
                    "call_id": payload.call_id,
                    "error": str(exc),
                },
            )
            return {
                "status": "post_error",
                "call_id": payload.call_id,
                "error": str(exc),
            }

    @staticmethod
    def _log_payload(payload: CallSummaryPayload) -> None:
        """Print the call summary payload to the terminal for visibility."""
        sep = "-" * 70
        lines = [
            "",
            f"{'--- CALL SUMMARY PAYLOAD ---':^70}",
            f"  subject             : {payload.subject}",
            sep,
            f"  call_id             : {payload.call_id}",
            f"  call_direction      : {payload.call_direction}",
            f"  agent_name          : {payload.agent_name}",
            f"  agent_number        : {payload.agent_number}",
            f"  caller_number       : {payload.caller_number}",
            f"  caller_name         : {payload.caller_name}",
            sep,
            f"  call_duration       : {payload.call_duration_seconds}s",
            f"  call_datetime_utc   : {payload.call_datetime_utc}",
            f"  notes_retry         : {payload.notes_retry_attempted}",
            sep,
            f"  body (notes)        : {payload.body[:200]!r}",
            "",
        ]
        logger.info("Sending call summary to Logics:\n" + "\n".join(lines))
