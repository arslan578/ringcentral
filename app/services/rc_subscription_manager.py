"""
app/services/rc_subscription_manager.py

Automatic RingCentral webhook subscription lifecycle manager.

PROBLEM SOLVED:
  RC webhook subscriptions EXPIRE (default ~15 minutes if expiresIn is
  not specified).  Once expired, RC stops sending notifications entirely
  — the server stays up but receives nothing.

  This manager:
    1. Creates a subscription at app startup (with long expiresIn)
    2. Runs a background renewal loop to keep it alive forever
    3. Handles blacklisted/expired subscriptions by recreating them
    4. Exposes status for the health endpoint

Requires the same JWT credentials already used by RCApiClient.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.rc_api_client import RCApiClient

logger = logging.getLogger(__name__)

# RC subscription API base path
_SUB_PATH = "/restapi/v1.0/subscription"

# Request 7 days expiration (in seconds).  RC may cap this lower,
# but we read the actual expirationTime from the response.
_DEFAULT_EXPIRES_IN = 604_800  # 7 days

# Renew when less than this many seconds remain before expiration
_RENEWAL_BUFFER_SECONDS = 3_600  # 1 hour

# How often the background loop checks (seconds)
_CHECK_INTERVAL_SECONDS = 600  # every 10 minutes


class SubscriptionStatus:
    """Snapshot of the current subscription state (for health endpoint)."""

    def __init__(self) -> None:
        self.subscription_id: Optional[str] = None
        self.status: str = "not_initialized"
        self.expiration_time: Optional[str] = None
        self.last_check_utc: Optional[str] = None
        self.last_error: Optional[str] = None
        self.delivery_url: Optional[str] = None
        self.renewals: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "status": self.status,
            "expiration_time": self.expiration_time,
            "last_check_utc": self.last_check_utc,
            "last_error": self.last_error,
            "delivery_url": self.delivery_url,
            "total_renewals": self.renewals,
        }


class RCSubscriptionManager:
    """
    Manages the RingCentral webhook subscription lifecycle.

    Usage:
        manager = RCSubscriptionManager(rc_api, delivery_url, verification_token)
        await manager.ensure_subscription()   # call at startup
        task = asyncio.create_task(manager.run_renewal_loop())  # background
    """

    def __init__(
        self,
        rc_api: RCApiClient,
        delivery_url: str,
        verification_token: str,
        event_filters: Optional[list[str]] = None,
        expires_in: int = _DEFAULT_EXPIRES_IN,
    ) -> None:
        self._rc_api = rc_api
        self._delivery_url = delivery_url.rstrip("/")
        self._verification_token = verification_token
        self._event_filters = event_filters or [
            "/restapi/v1.0/account/~/extension/~/message-store"
        ]
        self._expires_in = expires_in

        self.status = SubscriptionStatus()
        self.status.delivery_url = self._delivery_url

        self._renewal_task: Optional[asyncio.Task] = None

    # ── Public API ────────────────────────────────────────────────

    async def ensure_subscription(self) -> None:
        """
        Ensure an active webhook subscription exists.
        Creates a new one or renews an existing one as needed.
        """
        try:
            self.status.last_check_utc = datetime.now(timezone.utc).isoformat()

            # Step 1: List existing subscriptions
            existing = await self._list_subscriptions()

            # Step 2: Find ALL matching our delivery URL and deduplicate
            all_matching = self._find_all_matching_subscriptions(existing)

            # If multiple subscriptions exist for our URL, delete extras
            if len(all_matching) > 1:
                logger.warning(
                    "Multiple subscriptions found for same URL -- cleaning up duplicates",
                    extra={
                        "event": "subscription_duplicate_cleanup",
                        "count": len(all_matching),
                        "ids": [s.get("id") for s in all_matching],
                    },
                )
                # Keep the first active one, delete the rest
                kept = None
                for sub in all_matching:
                    if kept is None and sub.get("status", "").lower() == "active":
                        kept = sub
                    else:
                        await self._delete_subscription(str(sub.get("id", "")))
                # If none were active, kept is None and we'll create below
                all_matching = [kept] if kept else []

            matching = all_matching[0] if all_matching else None

            if matching:
                sub_status = matching.get("status", "").lower()
                sub_id = matching.get("id", "")

                if sub_status == "active":
                    # Check if it needs renewal
                    if self._needs_renewal(matching):
                        logger.info(
                            "Subscription needs renewal",
                            extra={
                                "event": "subscription_renewal_needed",
                                "subscription_id": sub_id,
                                "expiration_time": matching.get("expirationTime"),
                            },
                        )
                        await self._renew_subscription(sub_id)
                    else:
                        logger.info(
                            "Subscription is active and valid",
                            extra={
                                "event": "subscription_active",
                                "subscription_id": sub_id,
                                "expiration_time": matching.get("expirationTime"),
                            },
                        )
                        self._update_status_from_response(matching)

                elif sub_status in ("blacklisted", "suspended"):
                    logger.warning(
                        "Subscription is blacklisted/suspended -- deleting and recreating",
                        extra={
                            "event": "subscription_blacklisted",
                            "subscription_id": sub_id,
                            "status": sub_status,
                        },
                    )
                    await self._delete_subscription(sub_id)
                    await self._create_subscription()

                else:
                    # Unknown or expired status -- recreate
                    logger.warning(
                        "Subscription has unexpected status -- recreating",
                        extra={
                            "event": "subscription_unexpected_status",
                            "subscription_id": sub_id,
                            "status": sub_status,
                        },
                    )
                    await self._delete_subscription(sub_id)
                    await self._create_subscription()
            else:
                # No matching subscription found -- create new
                logger.info(
                    "No matching subscription found -- creating new",
                    extra={"event": "subscription_not_found"},
                )
                await self._create_subscription()

        except Exception as exc:
            error_msg = str(exc)
            logger.error(
                "Subscription management failed",
                extra={
                    "event": "subscription_management_error",
                    "error": error_msg,
                },
            )
            self.status.status = "error"
            self.status.last_error = error_msg

    async def run_renewal_loop(self) -> None:
        """
        Background loop that periodically checks and renews the subscription.
        Run this as an asyncio task.
        """
        logger.info(
            "Subscription renewal loop started",
            extra={
                "event": "renewal_loop_started",
                "check_interval_seconds": _CHECK_INTERVAL_SECONDS,
            },
        )

        while True:
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
            try:
                await self.ensure_subscription()
            except Exception as exc:
                logger.error(
                    "Renewal loop iteration failed",
                    extra={
                        "event": "renewal_loop_error",
                        "error": str(exc),
                    },
                )
                # Don't crash the loop -- retry next iteration

    def start_background_renewal(self) -> asyncio.Task:
        """Create and return the background renewal task."""
        self._renewal_task = asyncio.create_task(self.run_renewal_loop())
        return self._renewal_task

    def stop_background_renewal(self) -> None:
        """Cancel the background renewal task."""
        if self._renewal_task and not self._renewal_task.done():
            self._renewal_task.cancel()
            logger.info("Subscription renewal loop stopped")

    # ── Internal helpers ──────────────────────────────────────────

    async def _list_subscriptions(self) -> list[dict[str, Any]]:
        """List all webhook subscriptions via RC API."""
        token = await self._rc_api._ensure_token()
        url = f"{self._rc_api._server_url}{_SUB_PATH}"

        response = await self._rc_api._http.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=15.0,
        )

        if not response.is_success:
            logger.error(
                "Failed to list RC subscriptions",
                extra={
                    "event": "subscription_list_error",
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                },
            )
            return []

        data = response.json()
        records = data.get("records", [])
        logger.info(
            "Listed RC subscriptions",
            extra={
                "event": "subscription_list_success",
                "count": len(records),
            },
        )
        return records

    def _find_all_matching_subscriptions(
        self, subscriptions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Find ALL subscriptions matching our delivery URL."""
        matches = []
        for sub in subscriptions:
            delivery = sub.get("deliveryMode", {})
            address = delivery.get("address", "").rstrip("/")
            if address == self._delivery_url:
                matches.append(sub)
        return matches

    def _needs_renewal(self, subscription: dict[str, Any]) -> bool:
        """Check if a subscription needs renewal (close to expiration)."""
        exp_str = subscription.get("expirationTime")
        if not exp_str:
            return True

        try:
            exp_time = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            remaining = (exp_time - now).total_seconds()

            logger.debug(
                "Subscription expiration check",
                extra={
                    "remaining_seconds": remaining,
                    "renewal_buffer": _RENEWAL_BUFFER_SECONDS,
                    "needs_renewal": remaining < _RENEWAL_BUFFER_SECONDS,
                },
            )

            return remaining < _RENEWAL_BUFFER_SECONDS
        except (ValueError, TypeError):
            return True

    async def _create_subscription(self) -> None:
        """Create a new webhook subscription."""
        token = await self._rc_api._ensure_token()
        url = f"{self._rc_api._server_url}{_SUB_PATH}"

        body = {
            "eventFilters": self._event_filters,
            "deliveryMode": {
                "transportType": "WebHook",
                "address": self._delivery_url,
                "verificationToken": self._verification_token,
            },
            "expiresIn": self._expires_in,
        }

        logger.info(
            "Creating new RC webhook subscription",
            extra={
                "event": "subscription_creating",
                "delivery_url": self._delivery_url,
                "event_filters": self._event_filters,
                "expires_in_seconds": self._expires_in,
            },
        )

        response = await self._rc_api._http.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

        if response.is_success:
            data = response.json()
            self._update_status_from_response(data)
            logger.info(
                "RC webhook subscription created successfully",
                extra={
                    "event": "subscription_created",
                    "subscription_id": data.get("id"),
                    "status": data.get("status"),
                    "expiration_time": data.get("expirationTime"),
                },
            )
        else:
            error_text = response.text[:500]
            logger.error(
                "Failed to create RC subscription",
                extra={
                    "event": "subscription_create_error",
                    "status_code": response.status_code,
                    "response_body": error_text,
                },
            )
            self.status.status = "create_failed"
            self.status.last_error = f"HTTP {response.status_code}: {error_text}"

    async def _renew_subscription(self, subscription_id: str) -> None:
        """Renew an existing subscription."""
        token = await self._rc_api._ensure_token()
        url = f"{self._rc_api._server_url}{_SUB_PATH}/{subscription_id}/renew"

        logger.info(
            "Renewing RC webhook subscription",
            extra={
                "event": "subscription_renewing",
                "subscription_id": subscription_id,
            },
        )

        response = await self._rc_api._http.post(
            url,
            json={},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15.0,
        )

        if response.is_success:
            data = response.json()
            self._update_status_from_response(data)
            self.status.renewals += 1
            logger.info(
                "RC webhook subscription renewed successfully",
                extra={
                    "event": "subscription_renewed",
                    "subscription_id": data.get("id"),
                    "expiration_time": data.get("expirationTime"),
                    "total_renewals": self.status.renewals,
                },
            )
        else:
            error_text = response.text[:500]
            logger.warning(
                "Failed to renew subscription -- will recreate",
                extra={
                    "event": "subscription_renew_error",
                    "subscription_id": subscription_id,
                    "status_code": response.status_code,
                    "response_body": error_text,
                },
            )
            # Renewal failed (maybe blacklisted) -- delete and recreate
            await self._delete_subscription(subscription_id)
            await self._create_subscription()

    async def _delete_subscription(self, subscription_id: str) -> None:
        """Delete a subscription."""
        token = await self._rc_api._ensure_token()
        url = f"{self._rc_api._server_url}{_SUB_PATH}/{subscription_id}"

        logger.info(
            "Deleting RC subscription",
            extra={
                "event": "subscription_deleting",
                "subscription_id": subscription_id,
            },
        )

        response = await self._rc_api._http.delete(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=15.0,
        )

        if response.status_code in (200, 204):
            logger.info(
                "RC subscription deleted",
                extra={
                    "event": "subscription_deleted",
                    "subscription_id": subscription_id,
                },
            )
        else:
            logger.warning(
                "Failed to delete RC subscription",
                extra={
                    "event": "subscription_delete_error",
                    "subscription_id": subscription_id,
                    "status_code": response.status_code,
                },
            )

    def _update_status_from_response(self, data: dict[str, Any]) -> None:
        """Update the status snapshot from an RC API response."""
        self.status.subscription_id = str(data.get("id", ""))
        self.status.status = data.get("status", "unknown")
        self.status.expiration_time = data.get("expirationTime")
        self.status.last_error = None

