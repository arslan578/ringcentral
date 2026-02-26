"""
Quick diagnostic script to test RingCentral API authentication.
Run this locally to verify your JWT token and permissions work.

Usage:
    python scripts/test_rc_auth.py
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
RC_BEARER_TOKEN = os.getenv("RC_BEARER_TOKEN", "")

SEP = "-" * 60


def test_jwt_auth():
    """Test JWT authentication."""
    print(f"\n{SEP}")
    print("  TEST 1: JWT Authentication")
    print(SEP)
    print(f"  Server    : {RC_SERVER_URL}")
    print(f"  Client ID : {RC_CLIENT_ID[:10]}..." if RC_CLIENT_ID else "  Client ID : NOT SET")
    print(f"  JWT Token : {RC_JWT_TOKEN[:30]}..." if RC_JWT_TOKEN else "  JWT Token : NOT SET")
    print(SEP)

    if not RC_CLIENT_ID or not RC_CLIENT_SECRET or not RC_JWT_TOKEN:
        print("  SKIP -- Missing RC_CLIENT_ID, RC_CLIENT_SECRET, or RC_JWT_TOKEN")
        return None

    token_url = f"{RC_SERVER_URL}/restapi/oauth/token"

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": RC_JWT_TOKEN,
            },
            auth=(RC_CLIENT_ID, RC_CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    print(f"  Status: {response.status_code}")
    print()

    try:
        body = response.json()
        print(json.dumps(body, indent=2))
    except Exception:
        print(response.text)

    if response.is_success:
        token = body.get("access_token", "")
        print(f"\n  SUCCESS -- Got access token: {token[:30]}...")
        return token
    else:
        print(f"\n  FAILED -- {body.get('error_description', 'Unknown error')}")
        print()
        print("  HOW TO FIX:")
        print("  -----------")
        if "ThirdPartyAppAccess" in response.text:
            print("  This is a USER PERMISSION issue, not an app issue.")
            print()
            print("  The RingCentral ADMIN must do this:")
            print("    1. Log in to https://service.ringcentral.com")
            print("    2. Go to: Users > User List > select your user")
            print("    3. Go to: Roles and Permissions")
            print("    4. Enable 'Third-Party App Access'")
            print("    5. Save")
            print()
            print("  OR the admin can enable it globally:")
            print("    1. Log in to https://service.ringcentral.com")
            print("    2. Go to: Security > Authentication Settings")
            print("    3. Enable third-party app access for all users")
        return None


def test_bearer_token():
    """Test if the existing Bearer token can read messages."""
    print(f"\n{SEP}")
    print("  TEST 2: Bearer Token (from API Explorer)")
    print(SEP)
    print(f"  Bearer    : {RC_BEARER_TOKEN[:30]}..." if RC_BEARER_TOKEN else "  Bearer    : NOT SET")
    print(SEP)

    if not RC_BEARER_TOKEN:
        print("  SKIP -- RC_BEARER_TOKEN not set")
        return None

    url = f"{RC_SERVER_URL}/restapi/v1.0/account/~/extension/~/message-store"
    params = {"messageType": "SMS", "perPage": 1}

    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {RC_BEARER_TOKEN.strip()}",
                "Accept": "application/json",
            },
        )

    print(f"  Status: {response.status_code}")
    print()

    try:
        body = response.json()
        if response.is_success:
            records = body.get("records", [])
            print(f"  SUCCESS -- Found {len(records)} message(s)")
            if records:
                msg = records[0]
                print(f"  Latest message:")
                print(f"    ID        : {msg.get('id')}")
                print(f"    Type      : {msg.get('type')}")
                print(f"    Direction : {msg.get('direction')}")
                print(f"    From      : {msg.get('from', {}).get('phoneNumber', 'N/A')}")
                print(f"    To        : {[t.get('phoneNumber') for t in msg.get('to', [])]}")
                print(f"    Body      : {msg.get('subject', '')[:80]}")
                print(f"    Time      : {msg.get('creationTime')}")
            return RC_BEARER_TOKEN.strip()
        else:
            print(json.dumps(body, indent=2))
            if response.status_code == 401:
                print("\n  Bearer token expired. Get a new one from RC API Explorer.")
            return None
    except Exception:
        print(response.text)
        return None


def main():
    print("\n" + "=" * 60)
    print("  RingCentral API Authentication Diagnostic")
    print("=" * 60)

    # Test 1: JWT auth
    jwt_token = test_jwt_auth()

    # Test 2: Bearer token
    bearer_token = test_bearer_token()

    # Summary
    print(f"\n{'=' * 60}")
    print("  SUMMARY")
    print("=" * 60)

    if jwt_token:
        print("  JWT Auth    : WORKING")
    else:
        print("  JWT Auth    : FAILED (need ThirdPartyAppAccess permission)")

    if bearer_token:
        print("  Bearer Auth : WORKING (can read messages)")
        if not jwt_token:
            print()
            print("  RECOMMENDATION:")
            print("  Your Bearer token works but JWT does not.")
            print("  You have two options:")
            print()
            print("  Option A (Best): Get your RC admin to enable ThirdPartyAppAccess")
            print("            for your user at https://service.ringcentral.com")
            print()
            print("  Option B (Quick): Use the Bearer token directly.")
            print("            Add RC_BEARER_TOKEN to .env and we can")
            print("            use it instead of JWT auth.")
            print("            NOTE: Bearer tokens expire (usually 1 hour)")
    else:
        print("  Bearer Auth : FAILED or not set")

    print("=" * 60)


if __name__ == "__main__":
    main()

