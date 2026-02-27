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

