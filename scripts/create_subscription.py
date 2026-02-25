"""
Quick script to create a RingCentral webhook subscription.
Run: python scripts/create_subscription.py
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

if not BEARER_TOKEN:
    print("=" * 60)
    print("  Paste your Bearer token from the RC API Explorer")
    print("  Go to: developers.ringcentral.com -> API Reference")
    print("  Sign in -> copy the token from the curl command")
    print("=" * 60)
    BEARER_TOKEN = input("\nBearer Token: ").strip()

if not WEBHOOK_DELIVERY_URL:
    print("\n  Enter your full ngrok webhook URL:")
    print("  Example: https://xxxx.ngrok-free.app/api/v1/rc/webhook")
    WEBHOOK_DELIVERY_URL = input("\nWebhook URL: ").strip()

if not RC_WEBHOOK_VERIFICATION_TOKEN:
    RC_WEBHOOK_VERIFICATION_TOKEN = input("\nVerification Token (from .env): ").strip()

print("\n" + "=" * 60)
print("  Creating RingCentral Webhook Subscription")
print("=" * 60)
print(f"  RC Server : {RC_SERVER_URL}")
print(f"  Webhook   : {WEBHOOK_DELIVERY_URL}")
print(f"  Token     : {RC_WEBHOOK_VERIFICATION_TOKEN}")
print("=" * 60)

subscription_body = {
    "eventFilters": [
        "/restapi/v1.0/account/~/extension/~/message-store"
    ],
    "deliveryMode": {
        "transportType": "WebHook",
        "address": WEBHOOK_DELIVERY_URL,
        "verificationToken": RC_WEBHOOK_VERIFICATION_TOKEN,
    }
}

print(f"\nRequest JSON:\n{json.dumps(subscription_body, indent=2)}\n")

url = f"{RC_SERVER_URL}/restapi/v1.0/subscription"

try:
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            url,
            json=subscription_body,
            headers={
                "Authorization": f"Bearer {BEARER_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    print(f"Status: {response.status_code}")
    
    try:
        resp_json = response.json()
        print(f"\nResponse:\n{json.dumps(resp_json, indent=2)}")
    except Exception:
        print(f"\nResponse body:\n{response.text}")
        resp_json = {}

    if response.status_code in (200, 201):
        print("\n" + "=" * 60)
        print("  [SUCCESS] SUBSCRIPTION CREATED!")
        print("=" * 60)
        print(f"  ID        : {resp_json.get('id')}")
        print(f"  Status    : {resp_json.get('status')}")
        print(f"  Expires   : {resp_json.get('expirationTime')}")
        print(f"  Webhook   : {resp_json.get('deliveryMode', {}).get('address')}")
        print("=" * 60)
        print("\n  Your server will now receive ALL inbound SMS events!")
    elif response.status_code == 401:
        print("\n  [FAILED] Bearer token expired or invalid!")
        print("  Go to: developers.ringcentral.com/api-reference/Subscriptions/createSubscription")
        print("  Click 'Sign in to try it out' -> sign in again")
        print("  Copy the new Bearer token from the generated curl command")
        print("  Then run this script again with the new token.")
    else:
        print("\n  [FAILED] See error details above.")
        print("\n  Common fixes:")
        print("  - Bearer token expired? Get a new one from RC API Explorer")
        print("  - ngrok not running? Restart it")
        print("  - FastAPI server not running? Start it")

except Exception as e:
    print(f"\n[ERROR] Request error: {e}")
