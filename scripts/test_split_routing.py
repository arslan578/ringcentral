import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

INBOUND_URL  = os.environ["ZAPIER_INBOUND_WEBHOOK_URL"]
OUTBOUND_URL = os.environ["ZAPIER_OUTBOUND_WEBHOOK_URL"]
NOW = datetime.now(timezone.utc).isoformat()

BASE = {
    "source": "ringcentral", "message_type": "SMS",
    "timestamp_utc": NOW, "received_at_utc": NOW,
    "account_id": "315079026", "extension_id": "562216026",
    "subscription_id": "test", "conversation_id": "12345",
    "read_status": "Unread", "message_status": "Received",
    "priority": "Normal", "availability": "Alive",
    "attachment_count": 0, "message_uri": "https://rc.com/test",
    "rc_event_type": "/restapi/v1.0/...", "rc_event_uuid": "test-uuid",
    "all_to_numbers": "+19498004011", "all_to_names": "John Ha",
}

inbound = {
    **BASE,
    "event_type": "inbound_sms", "message_id": "TEST_IN_001",
    "direction": "Inbound",
    "from_number": "+12125556789", "from_name": None,
    "to_number": "+19498004011", "to_name": "John Ha",
    "body": "TEST inbound SMS from external phone.",
}

outbound = {
    **BASE,
    "event_type": "outbound_sms", "message_id": "TEST_OUT_001",
    "direction": "Outbound",
    "from_number": "+19498004011", "from_name": "John Ha",
    "to_number": "+12125556789", "to_name": None,
    "body": "TEST outbound SMS from RC user.",
    "message_status": "Sent",
}

sep = "=" * 65
print(sep)
print("  SPLIT ROUTING LIVE TEST")
print(sep)

results = []
for label, url, payload in [
    ("INBOUND  -> Inbound Zap",  INBOUND_URL,  inbound),
    ("OUTBOUND -> Outbound Zap", OUTBOUND_URL, outbound),
]:
    print(f"\n  [{label}]")
    print(f"  URL  : {url}")
    print(f"  From : {payload['from_number']}")
    print(f"  To   : {payload['to_number']}")
    print(f"  Body : {payload['body']}")
    r = httpx.post(url, json=payload, timeout=15.0)
    ok = r.status_code == 200
    print(f"  HTTP : {r.status_code}  -->  {'OK - Zapier accepted!' if ok else 'FAILED!'}")
    results.append(ok)

print()
print(sep)
if all(results):
    print("  RESULT: BOTH routes working -- data pushed to both Zaps!")
else:
    print("  RESULT: ONE OR BOTH FAILED -- check above")
print(sep)
