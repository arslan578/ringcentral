"""
app/services/rc_api_client.py

RingCentral REST API client for fetching full SMS message data.

RC's message-store webhook only sends change notifications containing
message IDs — NOT the actual message content (from, to, body, etc.).
This client authenticates via JWT grant and fetches full message objects
by ID so we can forward complete data to Zapier.

Authentication flow:
  - JWT grant:  POST /restapi/oauth/token  with grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
  - Access token is cached and auto-refreshed when expired.

Reference:
  https://developers.ringcentral.com/api-reference/Get-Message
  https://developers.ringcentral.com/guide/authentication/jwt-flow
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class RCApiClient:
    """
    Async RingCentral REST API client.
    Handles OAuth2 JWT authentication and message fetching.

    Rate-limit protection:
      - A global asyncio.Semaphore(1) ensures only ONE call-log API request
        is in-flight at any time.  This prevents the cascading-429 problem
        where dozens of concurrent handlers each hit the API simultaneously.
      - A global _rate_limit_until timestamp is set whenever ANY request
        receives a 429.  All subsequent requests sleep until the cooldown
        expires, so we never waste retries while rate-limited.
    """

    def __init__(
        self,
        server_url: str,
        client_id: str,
        client_secret: str,
        jwt_token: str,
        http_client: httpx.AsyncClient,
    ):
        self._server_url = server_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._jwt_token = jwt_token
        self._http = http_client

        # Token cache
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        # ── Global rate-limit protection ───────────────────────────
        # Only ONE call-log request at a time (prevents 429 avalanche)
        self._call_log_semaphore = asyncio.Semaphore(1)
        # Timestamp until which ALL requests should wait (set on 429)
        self._global_rate_limit_until: float = 0.0

    async def _ensure_token(self) -> str:
        """
        Return a valid access token, refreshing via JWT grant if expired.
        Thread-safe via asyncio.Lock.
        """
        async with self._token_lock:
            # If token still valid (with 60s buffer), reuse it
            if self._access_token and time.time() < (self._token_expires_at - 60):
                return self._access_token

            logger.info("Requesting new RC access token via JWT grant")

            token_url = f"{self._server_url}/restapi/oauth/token"

            response = await self._http.post(
                token_url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": self._jwt_token,
                },
                auth=(self._client_id, self._client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )

            if not response.is_success:
                logger.error(
                    "RC token request failed",
                    extra={
                        "event": "rc_auth_failed",
                        "status_code": response.status_code,
                        "response_body": response.text[:500],
                    },
                )
                raise RuntimeError(
                    f"RC OAuth token request failed: {response.status_code} — {response.text[:300]}"
                )

            token_data = response.json()
            self._access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 3600)
            self._token_expires_at = time.time() + expires_in

            logger.info(
                "RC access token acquired",
                extra={
                    "event": "rc_token_acquired",
                    "expires_in": expires_in,
                },
            )
            return self._access_token

    # ── Extension listing (for company-wide subscriptions) ──────

    async def list_extensions(
        self,
        account_id: str = "~",
        status_filter: str = "Enabled",
    ) -> list[dict[str, Any]]:
        """
        List all extensions in the account.

        GET /restapi/v1.0/account/{accountId}/extension?status={status}&perPage=1000

        Used to build per-extension event filters so the webhook
        subscription covers ALL users, not just the JWT owner.
        Returns a list of extension dicts.
        """
        token = await self._ensure_token()

        all_extensions: list[dict[str, Any]] = []
        page = 1
        per_page = 1000

        while True:
            url = (
                f"{self._server_url}/restapi/v1.0/account/{account_id}/extension"
                f"?status={status_filter}&perPage={per_page}&page={page}"
            )

            logger.info(
                "Listing account extensions",
                extra={
                    "event": "rc_list_extensions",
                    "page": page,
                    "per_page": per_page,
                },
            )

            try:
                response = await self._http.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    timeout=30.0,
                )

                if not response.is_success:
                    logger.error(
                        "Failed to list extensions",
                        extra={
                            "event": "rc_list_extensions_error",
                            "status_code": response.status_code,
                            "response_body": response.text[:500],
                        },
                    )
                    break

                data = response.json()
                records = data.get("records", [])
                all_extensions.extend(records)

                # Check pagination
                paging = data.get("paging", {})
                total_pages = paging.get("totalPages", 1)

                logger.info(
                    "Extensions page fetched",
                    extra={
                        "event": "rc_extensions_page",
                        "page": page,
                        "total_pages": total_pages,
                        "records_on_page": len(records),
                        "total_so_far": len(all_extensions),
                    },
                )

                if page >= total_pages:
                    break
                page += 1

            except (httpx.TimeoutException, httpx.RequestError) as exc:
                logger.error(
                    "Network error listing extensions",
                    extra={
                        "event": "rc_list_extensions_network_error",
                        "error": str(exc),
                    },
                )
                break

        logger.info(
            "Extension listing complete",
            extra={
                "event": "rc_extensions_listed",
                "total_extensions": len(all_extensions),
            },
        )
        return all_extensions

    # ── Message fetching ──────────────────────────────────────────

    async def get_message(
        self,
        account_id: str,
        extension_id: str,
        message_id: str,
    ) -> Optional[dict[str, Any]]:
        """
        Fetch a single message from the RC Message Store API.

        GET /restapi/v1.0/account/{accountId}/extension/{extensionId}/message-store/{messageId}

        Returns the full message dict or None if not found / error.
        """
        token = await self._ensure_token()

        url = (
            f"{self._server_url}/restapi/v1.0/account/{account_id}"
            f"/extension/{extension_id}/message-store/{message_id}"
        )

        logger.info(
            "Fetching message from RC API",
            extra={
                "event": "rc_api_fetch_message",
                "account_id": account_id,
                "extension_id": extension_id,
                "message_id": message_id,
            },
        )

        try:
            response = await self._http.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=15.0,
            )

            if response.status_code == 404:
                logger.warning(
                    "RC message not found",
                    extra={
                        "event": "rc_message_not_found",
                        "message_id": message_id,
                    },
                )
                return None

            if not response.is_success:
                logger.error(
                    "RC API error fetching message",
                    extra={
                        "event": "rc_api_error",
                        "message_id": message_id,
                        "status_code": response.status_code,
                        "response_body": response.text[:500],
                    },
                )
                return None

            msg_data = response.json()
            logger.info(
                "RC message fetched successfully",
                extra={
                    "event": "rc_message_fetched",
                    "message_id": message_id,
                    "direction": msg_data.get("direction"),
                    "type": msg_data.get("type"),
                },
            )
            return msg_data

        except httpx.TimeoutException as exc:
            logger.error(
                "RC API request timed out",
                extra={
                    "event": "rc_api_timeout",
                    "message_id": message_id,
                    "error": str(exc),
                },
            )
            return None

        except httpx.RequestError as exc:
            logger.error(
                "RC API network error",
                extra={
                    "event": "rc_api_network_error",
                    "message_id": message_id,
                    "error": str(exc),
                },
            )
            return None

    # ── Call Log fetching (for AI call notes) ─────────────────────

    # ── Global rate-limit helpers ────────────────────────────────

    async def _wait_for_global_cooldown(self) -> None:
        """
        If a previous request received 429, ALL subsequent requests must
        wait until the cooldown expires.  This prevents dozens of concurrent
        handlers from independently hammering the API during a rate-limit
        window.
        """
        now = time.time()
        if self._global_rate_limit_until > now:
            wait_time = self._global_rate_limit_until - now
            logger.info(
                "Global rate-limit cooldown active — waiting %.0fs before next API call",
                wait_time,
                extra={
                    "event": "rc_global_cooldown_wait",
                    "wait_seconds": round(wait_time),
                },
            )
            await asyncio.sleep(wait_time)

    def _set_global_cooldown(self, retry_after: int) -> None:
        """Set the global cooldown timestamp so ALL requests respect it."""
        new_until = time.time() + retry_after
        # Only extend, never shorten
        if new_until > self._global_rate_limit_until:
            self._global_rate_limit_until = new_until
            logger.info(
                "Global rate-limit cooldown set for %ds",
                retry_after,
                extra={
                    "event": "rc_global_cooldown_set",
                    "retry_after": retry_after,
                },
            )

    async def get_call_log_entry(
        self,
        account_id: str,
        call_id: str,
    ) -> Optional[dict[str, Any]]:
        """
        Fetch a single call log entry from the RC Call Log API.

        GET /restapi/v1.0/account/~/call-log/{callId}?view=Detailed

        We use `~` for account_id (= authenticated user's own account)
        instead of the numeric ID from the event, because the telephony
        events may arrive from a different sub-account ID, and `~` is
        always valid.

        Returns the full call record including:
          - parties[]: from/to numbers, names, directions
          - duration: int (seconds)
          - startTime: ISO-8601 timestamp
          - notes: AI-generated call notes text (if available)
          - recording: recording metadata (if available)

        Returns None if not found or on error.

        Rate‐limit handling:
          - A global semaphore ensures only ONE call-log request is in-flight.
          - A global cooldown timestamp prevents retries while rate-limited.
          - If RC returns 429, the Retry-After header is read and applied
            globally, then the request is retried up to 3 times.

        Reference:
          https://developers.ringcentral.com/api-reference/Call-Log/readCompanyCallRecord
        """
        # ── Acquire semaphore: only ONE call-log fetch at a time ──
        async with self._call_log_semaphore:
            return await self._fetch_call_log_inner(account_id, call_id)

    async def _fetch_call_log_inner(
        self,
        account_id: str,
        call_id: str,
    ) -> Optional[dict[str, Any]]:
        """Inner implementation — runs inside the semaphore."""
        # Respect global cooldown BEFORE making any request
        await self._wait_for_global_cooldown()

        token = await self._ensure_token()

        # Use '~' so the request goes against the authenticated account,
        # regardless of which sub-account the telephony event originated from.
        url = (
            f"{self._server_url}/restapi/v1.0/account/~"
            f"/call-log/{call_id}?view=Detailed"
        )

        logger.info(
            "Fetching call log entry from RC API",
            extra={
                "event": "rc_api_fetch_call_log",
                "account_id": account_id,
                "call_id": call_id,
            },
        )

        max_429_retries = 3

        for attempt in range(1, max_429_retries + 1):
            try:
                # Re-check global cooldown before each retry
                await self._wait_for_global_cooldown()

                response = await self._http.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    timeout=15.0,
                )

                if response.status_code == 404:
                    logger.warning(
                        "RC call log entry not found",
                        extra={
                            "event": "rc_call_log_not_found",
                            "call_id": call_id,
                        },
                    )
                    return None

                # ── Handle 429 Rate Limit ──────────────────────
                if response.status_code == 429:
                    # RC includes Retry-After header (seconds to wait)
                    retry_after_str = response.headers.get("Retry-After", "")
                    try:
                        retry_after = int(retry_after_str)
                    except (ValueError, TypeError):
                        retry_after = 60  # default 60s if header missing

                    # Cap at 120s to avoid excessively long waits
                    retry_after = min(retry_after, 120)

                    # Set GLOBAL cooldown so ALL other waiting requests
                    # also respect this rate limit (not just this one)
                    self._set_global_cooldown(retry_after)

                    if attempt < max_429_retries:
                        logger.warning(
                            "RC API rate limited (429) — waiting %ds before retry (attempt %d/%d)",
                            retry_after, attempt, max_429_retries,
                            extra={
                                "event": "rc_call_log_rate_limited",
                                "call_id": call_id,
                                "retry_after": retry_after,
                                "attempt": attempt,
                            },
                        )
                        await asyncio.sleep(retry_after)
                        # Re-acquire token in case it expired during wait
                        token = await self._ensure_token()
                        continue
                    else:
                        logger.error(
                            "RC API rate limited (429) — max retries exhausted",
                            extra={
                                "event": "rc_call_log_rate_limit_exhausted",
                                "call_id": call_id,
                                "attempts": max_429_retries,
                            },
                        )
                        return None

                if not response.is_success:
                    logger.error(
                        "RC API error fetching call log",
                        extra={
                            "event": "rc_call_log_api_error",
                            "call_id": call_id,
                            "status_code": response.status_code,
                            "response_body": response.text[:500],
                        },
                    )
                    return None

                call_data = response.json()
                logger.info(
                    "RC call log entry fetched successfully",
                    extra={
                        "event": "rc_call_log_fetched",
                        "call_id": call_id,
                        "duration": call_data.get("duration"),
                        "has_notes": bool(call_data.get("notes")),
                    },
                )
                return call_data

            except httpx.TimeoutException as exc:
                logger.error(
                    "RC Call Log API request timed out",
                    extra={
                        "event": "rc_call_log_timeout",
                        "call_id": call_id,
                        "error": str(exc),
                    },
                )
                return None

            except httpx.RequestError as exc:
                logger.error(
                    "RC Call Log API network error",
                    extra={
                        "event": "rc_call_log_network_error",
                        "call_id": call_id,
                        "error": str(exc),
                    },
                )
                return None

        return None

    async def get_messages_batch(
        self,
        account_id: str,
        extension_id: str,
        message_ids: list[str],
    ) -> list[dict[str, Any]]:
        """
        Fetch multiple messages concurrently.
        Returns a list of successfully fetched message dicts.
        """
        tasks = [
            self.get_message(account_id, extension_id, mid)
            for mid in message_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        messages = []
        for mid, result in zip(message_ids, results):
            if isinstance(result, Exception):
                logger.error(
                    "Exception fetching message",
                    extra={
                        "event": "rc_batch_fetch_error",
                        "message_id": mid,
                        "error": str(result),
                    },
                )
            elif result is not None:
                messages.append(result)
            else:
                logger.warning(
                    "Message fetch returned None",
                    extra={"event": "rc_batch_fetch_none", "message_id": mid},
                )

        return messages

