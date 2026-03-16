"""
scripts/reset_subscriptions.py

Force-deletes ALL existing RingCentral webhook subscriptions so the app
can recreate them from scratch with the latest event filters on next startup.

Usage:
    cd "d:\\A SOFTWARE STORIES\\ringcentral"
    .venv\\Scripts\\python scripts/reset_subscriptions.py

Why:
    The subscription manager reuses active subscriptions.  If new event
    filters were added (e.g. telephony/sessions) AFTER the subscription
    was originally created, the old subscription kept running without them.
    This script cleans up so the next startup creates a fresh subscription
    that includes ALL configured filters.
"""
from __future__ import annotations

import asyncio
import os
import sys

# Allow importing app modules from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()  # Load .env for credentials

import httpx

# ── Config from .env ─────────────────────────────────────────────
RC_SERVER_URL = os.getenv("RC_SERVER_URL", "https://platform.ringcentral.com")
RC_CLIENT_ID = os.getenv("RC_CLIENT_ID", "")
RC_CLIENT_SECRET = os.getenv("RC_CLIENT_SECRET", "")
RC_JWT_TOKEN = os.getenv("RC_JWT_TOKEN", "")

SUB_PATH = "/restapi/v1.0/subscription"


async def _get_token(client: httpx.AsyncClient) -> str:
    """Authenticate with RC JWT grant and return an access token."""
    print("\n[1/3] Authenticating with RingCentral...")
    resp = await client.post(
        f"{RC_SERVER_URL}/restapi/oauth/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": RC_JWT_TOKEN,
        },
        auth=(RC_CLIENT_ID, RC_CLIENT_SECRET),
        timeout=15.0,
    )
    if not resp.is_success:
        print(f"  ✗ Auth failed: HTTP {resp.status_code}")
        print(f"    {resp.text[:300]}")
        sys.exit(1)

    token = resp.json().get("access_token", "")
    print(f"  ✓ Token acquired (…{token[-8:]})")
    return token


async def _list_subscriptions(
    client: httpx.AsyncClient, token: str
) -> list[dict]:
    """List all webhook subscriptions."""
    print("\n[2/3] Listing all RingCentral webhook subscriptions...")
    resp = await client.get(
        f"{RC_SERVER_URL}{SUB_PATH}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=15.0,
    )
    if not resp.is_success:
        print(f"  ✗ Failed to list: HTTP {resp.status_code}")
        return []

    records = resp.json().get("records", [])
    print(f"  ✓ Found {len(records)} subscription(s)")
    return records


async def _delete_subscription(
    client: httpx.AsyncClient, token: str, sub_id: str
) -> bool:
    """Delete a single subscription by ID."""
    resp = await client.delete(
        f"{RC_SERVER_URL}{SUB_PATH}/{sub_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=15.0,
    )
    return resp.status_code in (200, 204)


async def main() -> None:
    """Delete all subscriptions, then dump their details first."""
    if not RC_CLIENT_ID or not RC_JWT_TOKEN:
        print("ERROR: RC_CLIENT_ID and RC_JWT_TOKEN must be set in .env")
        sys.exit(1)

    async with httpx.AsyncClient() as client:
        token = await _get_token(client)
        subs = await _list_subscriptions(client, token)

        if not subs:
            print("\n  Nothing to delete — no subscriptions found.")
            print("  Start the app and it will create a fresh subscription.")
            return

        # Show details before deleting
        for i, sub in enumerate(subs):
            sub_id = sub.get("id", "?")
            status = sub.get("status", "?")
            delivery = sub.get("deliveryMode", {})
            address = delivery.get("address", "?")
            filters = sub.get("eventFilters", [])
            exp = sub.get("expirationTime", "?")

            print(f"\n  Subscription #{i + 1}:")
            print(f"    ID           : {sub_id}")
            print(f"    Status       : {status}")
            print(f"    Delivery URL : {address}")
            print(f"    Expiration   : {exp}")
            print(f"    Filters ({len(filters)}):")
            for f in filters:
                print(f"      • {f}")

        # Confirm
        print(f"\n[3/3] Deleting {len(subs)} subscription(s)...")
        deleted = 0
        for sub in subs:
            sub_id = str(sub.get("id", ""))
            if sub_id:
                ok = await _delete_subscription(client, token, sub_id)
                symbol = "✓" if ok else "✗"
                print(f"  {symbol} {sub_id}")
                if ok:
                    deleted += 1

        print(f"\nDone! Deleted {deleted} / {len(subs)} subscription(s).")
        print("Now restart the app — it will create a new subscription")
        print("with the updated event filters (including telephony/sessions).")


if __name__ == "__main__":
    asyncio.run(main())
