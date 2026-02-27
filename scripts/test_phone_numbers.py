"""
Test that from_number and to_number are always captured from live RC API.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from dotenv import load_dotenv
from app.schemas.rc_message import RCMessage
from app.schemas.zapier_payload import ZapierPayload

load_dotenv()

RC_SERVER_URL = os.getenv("RC_SERVER_URL")
RC_CLIENT_ID = os.getenv("RC_CLIENT_ID")
RC_CLIENT_SECRET = os.getenv("RC_CLIENT_SECRET")
RC_JWT_TOKEN = os.getenv("RC_JWT_TOKEN")

sep = "=" * 70

with httpx.Client(timeout=30.0) as c:
    # Authenticate
    r = c.post(
        f"{RC_SERVER_URL}/restapi/oauth/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": RC_JWT_TOKEN},
        auth=(RC_CLIENT_ID, RC_CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = r.json()["access_token"]
    print("Token acquired OK")

    # Fetch 5 most recent SMS (mix of inbound + outbound)
    r2 = c.get(
        f"{RC_SERVER_URL}/restapi/v1.0/account/~/extension/~/message-store",
        params={"messageType": "SMS", "perPage": 5, "page": 1},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    msgs = r2.json().get("records", [])
    print(f"Fetched {len(msgs)} recent messages from RC")

print()
print(sep)
print("  PHONE NUMBER CAPTURE TEST  --  from_number + to_number + all_to_numbers")
print(sep)

all_ok = True
for i, raw_msg in enumerate(msgs, 1):
    msg = RCMessage.model_validate(raw_msg)
    payload = ZapierPayload.from_rc_message(
        message=msg,
        raw_message=raw_msg,
        account_id="315079026",
        extension_id="test",
        subscription_id="test",
        rc_event_type="test",
        rc_event_uuid="test",
    )
    d = payload.model_dump(mode="json")
    direction  = d["direction"]
    from_num   = d["from_number"]
    to_num     = d["to_number"]
    all_to     = d["all_to_numbers"]

    ok_from = bool(from_num)
    ok_to   = bool(to_num)

    print(f"  MSG {i} | {direction:<8} | id={d['message_id']}")
    print(f"    from_number    : {from_num!r}  {'OK' if ok_from else '*** MISSING ***'}")
    print(f"    to_number      : {to_num!r}  {'OK' if ok_to else '*** MISSING ***'}")
    print(f"    all_to_numbers : {all_to!r}")

    # Show raw RC data for comparison
    raw_from = raw_msg.get("from", {})
    raw_to   = raw_msg.get("to", [])
    print(f"    [RAW] from.phoneNumber={raw_from.get('phoneNumber')!r}  from.extensionNumber={raw_from.get('extensionNumber')!r}")
    for j, t in enumerate(raw_to):
        print(f"    [RAW] to[{j}].phoneNumber={t.get('phoneNumber')!r}  to[{j}].extensionNumber={t.get('extensionNumber')!r}")

    if not ok_from or not ok_to:
        all_ok = False
    print()

print(sep)
print(f"  RESULT: {'ALL NUMBERS CAPTURED OK' if all_ok else 'SOME NUMBERS MISSING - CHECK ABOVE'}")
print(sep)
