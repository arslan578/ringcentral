"""
scripts/rebuild_company_wide_subscription.py

Deletes the existing single-extension subscription and creates a new
company-wide one with all SMS-capable extensions.

Run: python scripts/rebuild_company_wide_subscription.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import httpx
except ImportError:
    os.system(f"{sys.executable} -m pip install httpx")
    import httpx

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

RC_SERVER_URL = os.getenv("RC_SERVER_URL", "https://platform.ringcentral.com")
RC_CLIENT_ID = os.getenv("RC_CLIENT_ID", "")
RC_CLIENT_SECRET = os.getenv("RC_CLIENT_SECRET", "")
RC_JWT_TOKEN = os.getenv("RC_JWT_TOKEN", "")
RC_WEBHOOK_DELIVERY_URL = os.getenv("RC_WEBHOOK_DELIVERY_URL", "")
RC_WEBHOOK_VERIFICATION_TOKEN = os.getenv("RC_WEBHOOK_VERIFICATION_TOKEN", "")

# RC allows max 20 event filters per subscription POST call, but we can
# work around it by creating multiple subscriptions (one per batch).
# Actually RC FREE/standard allows 20 per sub. Trial/Pro accounts allow more.
# We'll try all at once first, then batch if needed.
_MAX_FILTERS_PER_SUB = 200   # RC actually allows up to 200 in paid plans

SMS_CAPABLE_TYPES = {"User", "DigitalUser", "VirtualUser", "Department", "SharedLinesGroup"}

SEP = "=" * 65


def get_token(client: httpx.Client) -> str:
    resp = client.post(
        f"{RC_SERVER_URL}/restapi/oauth/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": RC_JWT_TOKEN,
        },
        auth=(RC_CLIENT_ID, RC_CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not resp.is_success:
        raise RuntimeError(f"JWT auth failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    print(f"  [OK] JWT auth success. Scopes: {data.get('scope', '')}")
    return data["access_token"]


def list_all_extensions(client: httpx.Client, token: str) -> list[dict]:
    all_exts = []
    page = 1
    while True:
        resp = client.get(
            f"{RC_SERVER_URL}/restapi/v1.0/account/~/extension",
            params={"status": "Enabled", "perPage": 1000, "page": page},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if not resp.is_success:
            print(f"  [ERROR] Failed to list extensions ({resp.status_code}): {resp.text[:300]}")
            break
        data = resp.json()
        records = data.get("records", [])
        all_exts.extend(records)
        paging = data.get("paging", {})
        if page >= paging.get("totalPages", 1):
            break
        page += 1
    return all_exts


def delete_all_subscriptions(client: httpx.Client, token: str):
    resp = client.get(
        f"{RC_SERVER_URL}/restapi/v1.0/subscription",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if not resp.is_success:
        print(f"  [WARN] Could not list subscriptions: {resp.status_code}")
        return

    subs = resp.json().get("records", [])
    print(f"  Found {len(subs)} existing subscription(s)")
    for sub in subs:
        sub_id = sub.get("id")
        del_resp = client.delete(
            f"{RC_SERVER_URL}/restapi/v1.0/subscription/{sub_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if del_resp.status_code in (200, 204):
            print(f"  [OK] Deleted subscription {sub_id}")
        else:
            print(f"  [WARN] Could not delete {sub_id}: HTTP {del_resp.status_code}")


def create_subscription(client: httpx.Client, token: str, event_filters: list[str]) -> dict:
    body = {
        "eventFilters": event_filters,
        "deliveryMode": {
            "transportType": "WebHook",
            "address": RC_WEBHOOK_DELIVERY_URL,
            "verificationToken": RC_WEBHOOK_VERIFICATION_TOKEN,
        },
        "expiresIn": 604800,  # 7 days
    }

    print(f"\n  Creating subscription with {len(event_filters)} event filters...")
    print(f"  Webhook URL: {RC_WEBHOOK_DELIVERY_URL}")
    print(f"  First 3 filters:")
    for f in event_filters[:3]:
        print(f"    {f}")
    if len(event_filters) > 3:
        print(f"    ... and {len(event_filters) - 3} more")

    resp = client.post(
        f"{RC_SERVER_URL}/restapi/v1.0/subscription",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=30.0,
    )

    print(f"\n  HTTP Status: {resp.status_code}")
    data = resp.json()

    if resp.is_success:
        filters_in_resp = data.get("eventFilters", [])
        print(f"  [OK] SUBSCRIPTION CREATED SUCCESSFULLY!")
        print(f"  ID              : {data.get('id')}")
        print(f"  Status          : {data.get('status')}")
        print(f"  Expires         : {data.get('expirationTime')}")
        print(f"  Filters created : {len(filters_in_resp)}")
    else:
        print(f"  [FAILED] Could not create subscription!")
        print(json.dumps(data, indent=2)[:800])

    return data


def run():
    print(f"\n{SEP}")
    print("  Rebuild Company-Wide RingCentral Subscription")
    print(SEP)

    if not RC_WEBHOOK_DELIVERY_URL:
        print("  [ERROR] RC_WEBHOOK_DELIVERY_URL is not set in .env!")
        return
    if not RC_WEBHOOK_VERIFICATION_TOKEN:
        print("  [ERROR] RC_WEBHOOK_VERIFICATION_TOKEN is not set in .env!")
        return

    print(f"  Webhook URL : {RC_WEBHOOK_DELIVERY_URL}")
    print(f"  Server      : {RC_SERVER_URL}")
    print(SEP)

    with httpx.Client(timeout=60.0) as client:

        # Step 1: Auth
        print("\n[1/4] Getting JWT access token...")
        try:
            token = get_token(client)
        except RuntimeError as e:
            print(f"  [FAILED] {e}")
            return

        # Step 2: List all extensions
        print("\n[2/4] Listing all account extensions...")
        extensions = list_all_extensions(client, token)
        print(f"  Total extensions found: {len(extensions)}")

        # Build SMS-capable filters
        filters = []
        for ext in extensions:
            ext_id = ext.get("id")
            ext_type = ext.get("type", "")
            if ext_id and (ext_type in SMS_CAPABLE_TYPES or ext_type == ""):
                filters.append(f"/restapi/v1.0/account/~/extension/{ext_id}/message-store")

        # Always add current user as safety net
        fallback = "/restapi/v1.0/account/~/extension/~/message-store"
        if fallback not in filters:
            filters.append(fallback)

        print(f"  SMS-capable extensions: {len(filters)}")

        if not filters:
            print("  [ERROR] No extensions found — cannot create subscription")
            return

        # Step 3: Delete existing subscriptions
        print("\n[3/4] Deleting existing subscriptions...")
        delete_all_subscriptions(client, token)

        # Step 4: Create new company-wide subscription
        print(f"\n[4/4] Creating new company-wide subscription...")

        if len(filters) <= _MAX_FILTERS_PER_SUB:
            result = create_subscription(client, token, filters)
            if result.get("id"):
                filters_in_result = result.get("eventFilters", [])
                print(f"\n{SEP}")
                print("  DONE! Company-wide subscription created.")
                print(f"  Monitors {len(filters_in_result)} extensions/phone numbers.")
                print(f"  All inbound AND outbound SMS from ALL users will now flow through.")
                print(SEP)
            else:
                # If it failed, it might be too many filters — try batching
                print("\n  Trying batch approach (splitting into groups of 20)...")
                batch_size = 20
                for i in range(0, len(filters), batch_size):
                    batch = filters[i:i + batch_size]
                    print(f"\n  -- Batch {i // batch_size + 1} ({len(batch)} filters) --")
                    create_subscription(client, token, batch)
        else:
            # Batch into groups
            batch_size = _MAX_FILTERS_PER_SUB
            print(f"  Too many filters for one sub ({len(filters)}), batching into groups of {batch_size}...")
            for i in range(0, len(filters), batch_size):
                batch = filters[i:i + batch_size]
                print(f"\n  -- Batch {i // batch_size + 1} ({len(batch)} filters) --")
                create_subscription(client, token, batch)


if __name__ == "__main__":
    run()
