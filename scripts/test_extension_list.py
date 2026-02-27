"""
Quick diagnostic: Tests JWT auth + extension listing + subscription status.
Run: python scripts/test_extension_list.py
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

SEP = "=" * 65


def run():
    print(f"\n{SEP}")
    print("  RingCentral Extension List & Subscription Diagnostic")
    print(SEP)
    print(f"  Server    : {RC_SERVER_URL}")
    print(f"  Client ID : {RC_CLIENT_ID[:12]}...")
    print(f"  JWT Token : {RC_JWT_TOKEN[:30]}...")
    print(SEP)

    with httpx.Client(timeout=30.0) as client:

        # -- STEP 1: Get access token via JWT
        print("\n[1/4] Authenticating via JWT grant...")
        auth_resp = client.post(
            f"{RC_SERVER_URL}/restapi/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": RC_JWT_TOKEN,
            },
            auth=(RC_CLIENT_ID, RC_CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if not auth_resp.is_success:
            print(f"  [FAILED] HTTP {auth_resp.status_code}")
            print(json.dumps(auth_resp.json(), indent=2))
            print("\n  JWT auth failed. Check RC_CLIENT_ID, RC_CLIENT_SECRET, RC_JWT_TOKEN in .env")
            return

        token_data = auth_resp.json()
        token = token_data["access_token"]
        print(f"  [OK] JWT Auth SUCCESS - token acquired")
        print(f"  Scopes : {token_data.get('scope', 'N/A')}")

        # -- STEP 2: Get current user info
        print("\n[2/4] Checking current user info...")
        me_resp = client.get(
            f"{RC_SERVER_URL}/restapi/v1.0/account/~/extension/~",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if me_resp.is_success:
            me = me_resp.json()
            print(f"  [OK] Current User:")
            print(f"     Name      : {me.get('name', 'N/A')}")
            print(f"     ExtNum    : {me.get('extensionNumber', 'N/A')}")
            print(f"     ExtID     : {me.get('id', 'N/A')}")
            print(f"     Type      : {me.get('type', 'N/A')}")
            print(f"     Status    : {me.get('status', 'N/A')}")
        else:
            print(f"  [WARN] Could not get user info ({me_resp.status_code})")

        # -- STEP 3: List ALL account extensions
        print("\n[3/4] Listing ALL account extensions...")
        ext_resp = client.get(
            f"{RC_SERVER_URL}/restapi/v1.0/account/~/extension?status=Enabled&perPage=100",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

        print(f"  HTTP Status: {ext_resp.status_code}")

        if ext_resp.is_success:
            data = ext_resp.json()
            records = data.get("records", [])
            paging = data.get("paging", {})
            total = paging.get("totalElements", len(records))
            print(f"  [OK] SUCCESS - {len(records)} extensions on this page / {total} total")

            sms_types = {"User", "DigitalUser", "VirtualUser", "Department", "SharedLinesGroup"}
            sms_capable = [r for r in records if r.get("type") in sms_types or r.get("type") == ""]
            print(f"  SMS-capable extensions: {len(sms_capable)}")
            print(f"  COMPANY-WIDE SUBSCRIPTIONS WILL WORK - {len(sms_capable)} event filters will be created.")
            print()
            print(f"  {'ExtID':<15} {'ExtNum':<10} {'Type':<22} {'Name'}")
            print(f"  {'-'*15} {'-'*10} {'-'*22} {'-'*30}")
            for ext in records:
                ext_id = str(ext.get("id", ""))
                ext_num = str(ext.get("extensionNumber", ""))
                ext_type = ext.get("type", "")
                ext_name = ext.get("name", "")
                marker = "[SMS]" if ext_type in sms_types else "     "
                print(f"  {marker} {ext_id:<13} {ext_num:<10} {ext_type:<22} {ext_name}")

            if total > len(records):
                print(f"\n  ... and {total - len(records)} more extensions (paginated)")

        elif ext_resp.status_code == 403:
            body = ext_resp.json()
            print(f"  [FAILED] PERMISSION DENIED (403)")
            print(f"  Error: {body.get('message', body.get('errorCode', 'Unknown'))}")
            print()
            print("  YOUR ACCOUNT DOES NOT HAVE PERMISSION TO LIST ALL EXTENSIONS")
            print("  Company-wide SMS capture CANNOT work with this JWT token.")
            print()
            print("  WHAT NEEDS TO HAPPEN:")
            print("  The RingCentral ADMIN must do ONE of these:")
            print("  A) Grant you 'Super Admin' or 'Account Admin' role in RC Admin Portal")
            print("  B) Provide their own JWT token for the app")
        else:
            body = {}
            try:
                body = ext_resp.json()
            except Exception:
                pass
            print(f"  [FAILED] UNEXPECTED ERROR ({ext_resp.status_code})")
            print(json.dumps(body, indent=2)[:500])

        # -- STEP 4: Check active subscriptions
        print(f"\n[4/4] Checking active webhook subscriptions...")
        sub_resp = client.get(
            f"{RC_SERVER_URL}/restapi/v1.0/subscription",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

        if sub_resp.is_success:
            subs = sub_resp.json().get("records", [])
            print(f"  Active subscriptions: {len(subs)}")
            for i, sub in enumerate(subs, 1):
                delivery = sub.get("deliveryMode", {})
                filters = sub.get("eventFilters", [])
                print(f"\n  Subscription #{i}:")
                print(f"    ID      : {sub.get('id')}")
                print(f"    Status  : {sub.get('status')}")
                print(f"    Expires : {sub.get('expirationTime')}")
                print(f"    Webhook : {delivery.get('address', 'N/A')}")
                print(f"    Filters ({len(filters)} total):")
                for f in filters[:15]:
                    print(f"      {f}")
                if len(filters) > 15:
                    print(f"      ... and {len(filters) - 15} more")

            if not subs:
                print("  [WARN] NO ACTIVE SUBSCRIPTIONS FOUND!")
                print("  The app has no webhook subscription - it will receive NO messages.")
        else:
            print(f"  [FAILED] Could not list subscriptions ({sub_resp.status_code})")

    print(f"\n{SEP}")
    print("  Diagnostic complete.")
    print(SEP)


if __name__ == "__main__":
    run()
