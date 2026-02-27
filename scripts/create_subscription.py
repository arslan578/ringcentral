"""
RingCentral Webhook Subscription Manager.

Actions:
  list    — Show all active webhook subscriptions
  delete  — Delete all webhook subscriptions (cleanup before re-creating)
  create  — Create a new webhook subscription for inbound SMS
  reset   — Delete all + create new (recommended for token mismatch fix)

Usage:
  python scripts/create_subscription.py              # interactive menu
  python scripts/create_subscription.py list         # list subscriptions
  python scripts/create_subscription.py reset        # delete all + create new
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import httpx
except ImportError:
    os.system(f"{sys.executable} -m pip install httpx")
    import httpx

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

RC_SERVER_URL = os.getenv("RC_SERVER_URL", "https://platform.ringcentral.com")
RC_WEBHOOK_VERIFICATION_TOKEN = os.getenv("RC_WEBHOOK_VERIFICATION_TOKEN", "")
WEBHOOK_DELIVERY_URL = os.getenv("RC_WEBHOOK_DELIVERY_URL", "")
BEARER_TOKEN = os.getenv("RC_BEARER_TOKEN", "")

BASE_URL = f"{RC_SERVER_URL}/restapi/v1.0/subscription"


def _get_auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _prompt_bearer_token():
    global BEARER_TOKEN
    if not BEARER_TOKEN:
        print("=" * 70)
        print("  You need a Bearer token from the RC API Explorer.")
        print()
        print("  1. Open this URL in your browser:")
        print("     https://developers.ringcentral.com/api-reference/Subscriptions/listSubscriptions")
        print("  2. Click 'Sign in to try it out' (top-right)")
        print("  3. Sign in with your RingCentral account")
        print("  4. Click 'Try it out' → 'Execute'")
        print("  5. In the generated curl command, copy the token after 'Bearer '")
        print("=" * 70)
        BEARER_TOKEN = input("\nPaste Bearer Token: ").strip()


def _prompt_webhook_url():
    global WEBHOOK_DELIVERY_URL
    if not WEBHOOK_DELIVERY_URL:
        print("\n  Enter your full public webhook URL:")
        print("  Example (DigitalOcean): https://your-app.ondigitalocean.app/api/v1/rc/webhook")
        print("  Example (ngrok):        https://xxxx.ngrok-free.app/api/v1/rc/webhook")
        WEBHOOK_DELIVERY_URL = input("\nWebhook URL: ").strip()


def _prompt_verification_token():
    global RC_WEBHOOK_VERIFICATION_TOKEN
    if not RC_WEBHOOK_VERIFICATION_TOKEN:
        RC_WEBHOOK_VERIFICATION_TOKEN = input("\nVerification Token (from .env on server): ").strip()


# ─────────────────────────────────────────────────────────────────
# Actions
# ─────────────────────────────────────────────────────────────────

def list_subscriptions() -> list[dict]:
    """List all active webhook subscriptions."""
    print("\n" + "=" * 70)
    print("  Listing All Webhook Subscriptions")
    print("=" * 70)

    with httpx.Client(timeout=60.0) as client:
        response = client.get(BASE_URL, headers=_get_auth_headers())

    if response.status_code == 401:
        print("\n  [ERROR] Bearer token expired or invalid! Get a new one.")
        return []

    data = response.json()
    records = data.get("records", [])

    if not records:
        print("\n  No active subscriptions found.")
        return []

    for i, sub in enumerate(records, 1):
        delivery = sub.get("deliveryMode", {})
        print(f"\n  ── Subscription #{i} ──")
        print(f"  ID        : {sub.get('id')}")
        print(f"  Status    : {sub.get('status')}")
        print(f"  Transport : {delivery.get('transportType')}")
        print(f"  Webhook   : {delivery.get('address', 'N/A')}")
        print(f"  Created   : {sub.get('creationTime')}")
        print(f"  Expires   : {sub.get('expirationTime')}")
        filters = sub.get("eventFilters", [])
        print(f"  Events    : {', '.join(filters)}")

    print(f"\n  Total: {len(records)} subscription(s)")
    return records


def delete_all_subscriptions() -> int:
    """Delete ALL webhook subscriptions. Returns count deleted."""
    print("\n" + "=" * 70)
    print("  Deleting All Webhook Subscriptions")
    print("=" * 70)

    with httpx.Client(timeout=60.0) as client:
        # First, list them
        response = client.get(BASE_URL, headers=_get_auth_headers())
        if response.status_code == 401:
            print("\n  [ERROR] Bearer token expired or invalid!")
            return 0

        records = response.json().get("records", [])
        if not records:
            print("\n  No subscriptions to delete.")
            return 0

        deleted = 0
        for sub in records:
            sub_id = sub.get("id")
            del_url = f"{BASE_URL}/{sub_id}"
            del_resp = client.delete(del_url, headers=_get_auth_headers())
            if del_resp.status_code in (200, 204):
                print(f"  ✓ Deleted subscription {sub_id}")
                deleted += 1
            else:
                print(f"  ✗ Failed to delete {sub_id} (HTTP {del_resp.status_code})")

    print(f"\n  Deleted {deleted}/{len(records)} subscription(s)")
    return deleted


def create_subscription() -> bool:
    """Create a new webhook subscription for inbound SMS. Returns True on success."""
    _prompt_webhook_url()
    _prompt_verification_token()

    print("\n" + "=" * 70)
    print("  Creating RingCentral Webhook Subscription")
    print("=" * 70)
    print(f"  RC Server : {RC_SERVER_URL}")
    print(f"  Webhook   : {WEBHOOK_DELIVERY_URL}")
    print(f"  Token     : {RC_WEBHOOK_VERIFICATION_TOKEN}")
    print("=" * 70)

    subscription_body = {
        "eventFilters": [
            "/restapi/v1.0/account/~/extension/~/message-store"
        ],
        "deliveryMode": {
            "transportType": "WebHook",
            "address": WEBHOOK_DELIVERY_URL,
            "verificationToken": RC_WEBHOOK_VERIFICATION_TOKEN,
        },
        "expiresIn": 604800,  # 7 days (in seconds) — previously missing, causing 15-min default expiry!
    }

    print(f"\nRequest JSON:\n{json.dumps(subscription_body, indent=2)}\n")

    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            BASE_URL,
            json=subscription_body,
            headers=_get_auth_headers(),
        )

    print(f"Status: {response.status_code}")

    try:
        resp_json = response.json()
        print(f"\nResponse:\n{json.dumps(resp_json, indent=2)}")
    except Exception:
        print(f"\nResponse body:\n{response.text}")
        resp_json = {}

    if response.status_code in (200, 201):
        print("\n" + "=" * 70)
        print("  ✅ SUBSCRIPTION CREATED SUCCESSFULLY!")
        print("=" * 70)
        print(f"  ID        : {resp_json.get('id')}")
        print(f"  Status    : {resp_json.get('status')}")
        print(f"  Expires   : {resp_json.get('expirationTime')}")
        print(f"  Webhook   : {resp_json.get('deliveryMode', {}).get('address')}")
        print("=" * 70)
        print("\n  Your server will now receive ALL inbound SMS events!")
        return True
    elif response.status_code == 401:
        print("\n  [FAILED] Bearer token expired or invalid!")
        print("  Get a new one from:")
        print("  https://developers.ringcentral.com/api-reference/Subscriptions/createSubscription")
        return False
    else:
        print("\n  [FAILED] See error details above.")
        print("\n  Common fixes:")
        print("  - Bearer token expired? Get a new one from RC API Explorer")
        print("  - Server not reachable? RC must be able to reach your webhook URL")
        print("  - Check that your FastAPI server is running on DigitalOcean")
        return False


def reset_subscription() -> bool:
    """Delete all existing subscriptions and create a fresh one."""
    print("\n" + "=" * 70)
    print("  RESET: Delete old subscriptions + Create new one")
    print("  (This fixes Verification-Token mismatch issues)")
    print("=" * 70)

    delete_all_subscriptions()
    print()
    return create_subscription()


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

def main():
    action = sys.argv[1].lower() if len(sys.argv) > 1 else None

    if not action:
        print("\n  RC Webhook Subscription Manager")
        print("  ─────────────────────────────────")
        print("  1. list   — Show all subscriptions")
        print("  2. delete — Delete all subscriptions")
        print("  3. create — Create new subscription")
        print("  4. reset  — Delete all + create new (RECOMMENDED for token fix)")
        print()
        choice = input("  Choose action (1-4 or name): ").strip().lower()
        action_map = {"1": "list", "2": "delete", "3": "create", "4": "reset"}
        action = action_map.get(choice, choice)

    _prompt_bearer_token()

    try:
        if action == "list":
            list_subscriptions()
        elif action == "delete":
            delete_all_subscriptions()
        elif action == "create":
            create_subscription()
        elif action == "reset":
            reset_subscription()
        else:
            print(f"\n  Unknown action: {action}")
            print("  Valid actions: list, delete, create, reset")
    except Exception as e:
        print(f"\n  [ERROR] {e}")


if __name__ == "__main__":
    main()
