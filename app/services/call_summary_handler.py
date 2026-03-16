"""
app/services/call_summary_handler.py

Handles the RingCentral telephony call-ended event.

Flow:
  1. Receive the raw call-ended webhook payload (already parsed as dict).
  2. Extract call_id (telephonySessionId) and the agent identity.
  3. Wait a short period for the call log to become available in RC.
  4. Call the RC Call Log API to fetch AI notes for that call.
  5. If notes are not yet ready (AI takes time), retry with backoff.
  6. Build a CallSummaryPayload and POST it to the Logics endpoint.
  7. Always return (never raise) — log errors and carry on.

This handler runs ALONGSIDE the existing SMS webhook logic, not instead of it.
A call-ended event has a different event path from an SMS notification, so the
two code paths never overlap.

RC Call-Ended Event Path:
  /restapi/v1.0/account/{accountId}/telephony/sessions

RC Call Log API:
  GET /restapi/v1.0/account/{accountId}/call-log/{sessionId}?view=Detailed

Notes field location in call log:
  call_log.notes  (string, may be absent if AI hasn't finished processing)

Retry strategy:
  - Wait 10s before the first fetch (call log needs time to appear)
  - If notes are empty or API returns 429, wait 30s then retry
  - If still empty / 429, wait 60s then do a final retry
  - If still no notes after 3 attempts, send fallback "(AI notes not available)"
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from app.schemas.call_summary_payload import CallSummaryPayload
from app.services.rc_api_client import RCApiClient

logger = logging.getLogger(__name__)

# Retry schedule: delays before each fetch attempt (seconds)
# Attempt 1: wait 10s  (call log needs time to appear after disconnect)
# Attempt 2: wait 30s  (AI notes may still be processing)
# Attempt 3: wait 60s  (final attempt with longer wait)
_DEFAULT_RETRY_SCHEDULE: list[float] = [10.0, 30.0, 60.0]

# RC "queue" / IVR extension names that should NOT be used as the agent name.
# If ANY of these appear as a substring in the name (case-insensitive), it's
# considered a non-agent name.
_NON_AGENT_NAME_KEYWORDS: list[str] = [
    "main company number", "company number", "main number",
    "ivr", "auto receptionist", "auto-receptionist",
    "department", "queue", "call queue",
    "hold", "parking", "park location",
    "announcement", "paging",
]


def _is_real_agent_name(name: Optional[str]) -> bool:
    """
    Return True if the name looks like a real person, not an IVR/queue.

    Examples of NON-agent names:
      "Main Company Number", "1d. Billing Department", "Auto Receptionist"
    """
    if not name:
        return False

    lower = name.strip().lower()
    if not lower:
        return False

    # If any non-agent keyword appears anywhere in the name, it's not a real agent
    for keyword in _NON_AGENT_NAME_KEYWORDS:
        if keyword in lower:
            return False

    return True


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

    # First pass: find a party with extensionId whose name is a real person
    for party in parties:
        if not party.get("extensionId"):
            continue

        direction = party.get("direction", "")
        from_info = party.get("from") or {}
        to_info = party.get("to") or {}

        if direction == "Inbound":
            candidate_name = to_info.get("name") or party.get("name")
            candidate_number = (
                to_info.get("phoneNumber")
                or to_info.get("extensionNumber")
                or str(party.get("extensionId", ""))
            )
            ext_caller_number = from_info.get("phoneNumber") or from_info.get("extensionNumber")
            ext_caller_name = from_info.get("name")
        else:
            candidate_name = from_info.get("name") or party.get("name")
            candidate_number = (
                from_info.get("phoneNumber")
                or from_info.get("extensionNumber")
                or str(party.get("extensionId", ""))
            )
            ext_caller_number = to_info.get("phoneNumber") or to_info.get("extensionNumber")
            ext_caller_name = to_info.get("name")

        # Prefer a real agent name over IVR/queue names
        if _is_real_agent_name(candidate_name):
            agent_name = candidate_name
            agent_number = candidate_number
            caller_number = ext_caller_number
            caller_name = ext_caller_name
            call_direction = direction
            break
        elif not agent_name:
            # Keep as fallback if no better candidate found
            agent_name = candidate_name
            agent_number = candidate_number
            caller_number = ext_caller_number
            caller_name = ext_caller_name
            call_direction = direction

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

    Concurrency control:
      An asyncio.Lock serializes ALL call-ended processing so only ONE
      call is being fetched/posted at any given moment.  This prevents
      dozens of concurrent handlers from all hitting the RC API at once
      and causing a cascading 429 rate-limit avalanche.

      Combined with the per-request semaphore in RCApiClient, this
      guarantees the RC rate limits are respected.
    """

    def __init__(
        self,
        rc_api: RCApiClient,
        http_client: httpx.AsyncClient,
        logics_url: str,
        retry_schedule: list[float] | None = None,
    ) -> None:
        self._rc_api = rc_api
        self._http = http_client
        self._logics_url = logics_url
        self._retry_schedule = retry_schedule if retry_schedule is not None else _DEFAULT_RETRY_SCHEDULE

        # ── Serialize call processing ─────────────────────────────
        # Only ONE call-ended event is processed at a time.
        # Others queue behind this lock automatically.
        self._processing_lock = asyncio.Lock()

    async def handle(self, raw_body: dict[str, Any]) -> dict[str, Any]:
        """
        Process a call-ended webhook event (serialized).

        Acquires _processing_lock so only one event is processed at a time.
        This is critical: without it, dozens of concurrent handlers blast
        the RC API simultaneously, causing 429 cascades.

        Steps:
          1. Extract call_id and agent info from the payload.
          2. Fetch the call log entry with retries and backoff.
          3. Build CallSummaryPayload and POST to Logics endpoint.
          4. Return a status dict (never raises — errors are logged).

        Args:
            raw_body: The full decoded webhook request body dict.

        Returns:
            dict with 'status' and contextual fields.
        """
        try:
            async with self._processing_lock:
                return await self._process_call(raw_body)
        except Exception as exc:
            logger.error(
                "Unexpected error in call summary handler",
                extra={
                    "event": "call_summary_unexpected_error",
                    "error": str(exc),
                },
                exc_info=True,
            )
            return {"status": "error", "reason": str(exc)}

    async def _process_call(self, raw_body: dict[str, Any]) -> dict[str, Any]:
        """
        Inner processing logic — runs inside the _processing_lock.
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

        # ── Step 2: Fetch call log with retries ──────────────────
        call_log: dict[str, Any] | None = None
        notes = ""
        total_attempts = len(self._retry_schedule)

        for attempt_idx, delay in enumerate(self._retry_schedule):
            attempt_num = attempt_idx + 1

            if delay > 0:
                logger.info(
                    "Waiting %ss before call log fetch (attempt %d/%d)",
                    delay, attempt_num, total_attempts,
                    extra={
                        "event": "call_summary_waiting",
                        "call_id": call_id,
                        "attempt": attempt_num,
                        "delay": delay,
                    },
                )
                await asyncio.sleep(delay)

            call_log = await self._rc_api.get_call_log_entry(
                account_id=account_id,
                call_id=call_id,
            )

            if call_log:
                notes = _extract_notes_from_call_log(call_log)
                if notes:
                    logger.info(
                        "AI notes fetched on attempt %d/%d",
                        attempt_num, total_attempts,
                        extra={
                            "event": "call_summary_notes_fetched",
                            "call_id": call_id,
                            "attempt": attempt_num,
                            "notes_length": len(notes),
                        },
                    )
                    break
                else:
                    logger.info(
                        "Call log fetched but AI notes not yet available (attempt %d/%d)",
                        attempt_num, total_attempts,
                        extra={
                            "event": "call_summary_notes_not_ready",
                            "call_id": call_id,
                            "attempt": attempt_num,
                        },
                    )
            else:
                logger.warning(
                    "Call log fetch failed (attempt %d/%d) — will retry",
                    attempt_num, total_attempts,
                    extra={
                        "event": "call_summary_fetch_failed",
                        "call_id": call_id,
                        "attempt": attempt_num,
                    },
                )

        retry_attempted = total_attempts > 1

        if not notes:
            logger.warning(
                "AI notes not available after %d attempt(s) — using fallback",
                total_attempts,
                extra={
                    "event": "call_summary_notes_unavailable",
                    "call_id": call_id,
                    "attempts": total_attempts,
                },
            )

        # ── Step 3: Extract remaining metadata from call log ─────
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

            # Refine agent/caller from call log legs if event data was sparse
            if not _is_real_agent_name(agent_name) or not caller_number:
                log_from = call_log.get("from") or {}
                log_to = call_log.get("to") or {}
                log_direction = call_log.get("direction", "")

                # Try top-level from/to first
                if log_direction == "Inbound":
                    if _is_real_agent_name(log_to.get("name")):
                        agent_name = log_to.get("name")
                    if not caller_number:
                        caller_number = log_from.get("phoneNumber")
                    if not caller_name:
                        caller_name = log_from.get("name")
                elif log_direction == "Outbound":
                    if _is_real_agent_name(log_from.get("name")):
                        agent_name = log_from.get("name")
                    if not caller_number:
                        caller_number = log_to.get("phoneNumber")
                    if not caller_name:
                        caller_name = log_to.get("name")

                # Try legs as fallback
                log_legs = call_log.get("legs") or []
                for leg in log_legs:
                    leg_from = leg.get("from") or {}
                    leg_to = leg.get("to") or {}
                    leg_dir = leg.get("direction", "")

                    if not _is_real_agent_name(agent_name):
                        if leg_dir == "Inbound" and _is_real_agent_name(leg_to.get("name")):
                            agent_name = leg_to.get("name")
                        elif _is_real_agent_name(leg_from.get("name")):
                            agent_name = leg_from.get("name")
                    if not caller_number:
                        caller_number = leg_from.get("phoneNumber") or leg_to.get("phoneNumber")
                    if not call_direction:
                        call_direction = leg_dir

        # ── Step 4: Build payload ────────────────────────────────
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

        # ── Step 5: POST to Logics endpoint ──────────────────────
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
