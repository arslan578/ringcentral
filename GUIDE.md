# RC Inbound SMS → Zapier Webhook Integration
## Developer & Operator Guide

---

## 1. What This Application Does

This FastAPI server sits between **RingCentral (RC)** and **Zapier**.

```
RingCentral Platform
  └── inbound SMS arrives on any RC user's number
         │
         ▼ HTTPS POST (real-time)
┌─────────────────────────────┐
│  This FastAPI Server        │
│  /api/v1/rc/webhook         │
│                             │
│  1. Validates RC token      │
│  2. Deduplicates by msg ID  │
│  3. Builds full metadata    │
│  4. Retries up to 3×        │
└─────────────────────────────┘
         │
         ▼ HTTPS POST (near real-time)
Zapier Webhook → Your Zap → DNC Detection Logic
```

Every inbound SMS received across all RC users is captured and forwarded — **no filtering, no message loss**.

---

## 2. Prerequisites

| Requirement | Notes |
|---|---|
| Python **3.12+** | `python --version` to verify |
| pip | Comes with Python |
| A **RingCentral Developer** account | [developers.ringcentral.com](https://developers.ringcentral.com) |
| A **public HTTPS URL** | ngrok (local dev) or a cloud host (production) |
| Your `.env` file configured | See Section 3 |

---

## 3. First-Time Setup

### Step 1 — Clone and enter the project
```powershell
cd "d:\A SOFTWARE STORIES\ringcentral"
```

### Step 2 — Create a Python virtual environment
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### Step 3 — Install dependencies
```powershell
pip install -r requirements.txt
```

### Step 4 — Create your `.env` file
```powershell
copy .env.example .env
```
Open `.env` and fill in the two required values:

```env
RC_WEBHOOK_VERIFICATION_TOKEN=<choose any random string, e.g. a UUID>
ZAPIER_WEBHOOK_URL=https://hooks.zapier.com/hooks/catch/XXXXXXX/XXXXXXX/
```

> **IMPORTANT**: `RC_WEBHOOK_VERIFICATION_TOKEN` is a string **you choose**. You will enter this same string into the RingCentral Developer Console when creating the webhook subscription (see Section 5). RC will then send it on every push so we can verify authenticity.

---

## 4. Running the Server

### Local Development 
```powershell
# Make sure .venv is activated
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```
- API docs: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) *(development only)*
- Health check: [http://127.0.0.1:8000/api/v1/health](http://127.0.0.1:8000/api/v1/health)

### Production (Direct)
```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

### Production (Docker)
```powershell
# Build and start
docker-compose up -d --build

# View real-time logs
docker-compose logs -f web

# Stop
docker-compose down
```

---

## 5. Exposing the Server to RingCentral (Your Action Required)

RingCentral's servers **cannot reach localhost**. You must expose your server publicly.

### Option A — ngrok (Local Dev / Testing)

1. Download ngrok: [ngrok.com/download](https://ngrok.com/download)
2. Start your FastAPI server (Section 4)
3. In a second terminal:
   ```powershell
   ngrok http 8000
   ```
4. Copy the `https://xxxxx.ngrok-free.app` URL from ngrok output.

### Option B — Cloud Deployment (Production)

Recommended platforms:
- **Railway** — `railway up` (simplest, supports Docker)
- **Render** — Connect GitHub repo, auto-deploys on push
- **AWS EC2 / DigitalOcean VPS** — Run docker-compose behind nginx

Your public URL will be the domain provided by the platform.

---

## 6. Registering the Webhook in RingCentral (Your Action Required)

> Do this **after** you have a public HTTPS URL from Section 5.

1. Go to [developers.ringcentral.com](https://developers.ringcentral.com) and **sign in**.
2. Click **Console** → Select your app (or create one with SMS read scope).
3. Navigate to **Subscriptions** (or use the RC API Sandbox).
4. Create a new subscription:
   - **Address (Delivery URL):** `https://YOUR-PUBLIC-URL/api/v1/rc/webhook`
   - **Events:** `/restapi/v1.0/account/~/extension/~/message-store`
   - **Verification Token:** The exact same value you put in `RC_WEBHOOK_VERIFICATION_TOKEN` in your `.env`
5. Save. RC will immediately send a `GET` validation challenge to your URL.
   - ✅ If your server is running, it responds automatically.
   - You'll see `"RC webhook validation challenge received"` in your logs.
6. Your subscription is now active. **All inbound SMS across all extensions will trigger a POST to your endpoint.**

---

## 7. Verifying It Works

### Check Logs
```powershell
# Development (stdout)
# Look for:
{"event": "zapier_forward_success", "message_id": "...", "zapier_status_code": 200}

# Production (file)
tail -f logs\app.log
```

### Test the Health Endpoint
```powershell
curl http://localhost:8000/api/v1/health
# Expected: {"status": "ok", "service": "rc-sms-webhook", ...}
```

### Send a Test SMS
1. Send an SMS to any phone number assigned to your RC account.
2. Watch the logs — you should see a `zapier_forward_success` event within ~1 second.
3. In Zapier, go to your Zap → **Webhook History** → verify the payload arrived.

### Test Retry Behavior
1. Temporarily change `ZAPIER_WEBHOOK_URL=https://httpstat.us/500` in `.env`
2. Restart the server
3. Send a test SMS
4. Watch logs — you'll see 3 attempts with delays: `1s → 2s → 4s`
5. Restore the real Zapier URL and restart

---

## 8. JSON Payload Structure (Sent to Zapier)

Every Zapier call is an HTTPS POST with this JSON body:

```json
{
  "source": "ringcentral",
  "event_type": "inbound_sms",
  "message_id": "RC message unique ID",
  "direction": "Inbound",
  "from_number": "+15550001111",
  "to_number": "+15559990000",
  "body": "SMS message text here",
  "timestamp_utc": "2026-02-25T01:00:00+00:00",
  "received_at_utc": "2026-02-25T01:00:01+00:00",
  "account_id": "RC account ID",
  "extension_id": "RC extension ID",
  "subscription_id": "RC subscription ID",
  "conversation_id": "RC thread/conversation ID",
  "read_status": "Unread",
  "message_status": "Received",
  "delivery_error_code": null,
  "priority": "Normal",
  "availability": "Alive",
  "attachment_count": 0,
  "rc_event_type": "/restapi/v1.0/account/~/extension/~/message-store",
  "rc_event_uuid": "RC notification UUID",
  "raw_rc_payload": { "...complete original RC payload..." }
}
```

> `raw_rc_payload` contains the full, unmodified RC notification body — all metadata fields, even undocumented ones.

---

## 9. Running Tests

```powershell
# Install test dependencies
pip install -r requirements-dev.txt

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=app --cov-report=term-missing
```

**Expected output:** All tests pass. The `test_ttl_expiry_removes_entry` test takes ~1.5 seconds (intentional TTL sleep).

---

## 10. Log File Reference

Logs are written to `logs/app.log` (daily rotation, 7 days kept) and stdout.

| `event` field | Meaning |
|---|---|
| `rc_validation_challenge` | RC registered the webhook subscription |
| `rc_auth_rejected` | Incoming request had wrong/missing token |
| `zapier_forward_attempt` | Starting a Zapier POST |
| `zapier_forward_success` | Zapier returned 2xx |
| `zapier_forward_retry` | Zapier returned non-2xx, retrying |
| `zapier_forward_timeout` | HTTP timeout, retrying |
| `zapier_forward_failed` | All 3 retries exhausted — message NOT forwarded |
| `duplicate_suppressed` | Same message ID seen within 24h |
| `event_filtered` | Non-SMS or outbound event — ignored |

---

## 11. Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `RC_WEBHOOK_VERIFICATION_TOKEN` | ✅ Yes | — | Your chosen secret token. Must match what's in RC Console. |
| `ZAPIER_WEBHOOK_URL` | ✅ Yes | — | Zapier catch-hook HTTPS URL |
| `APP_ENV` | No | `production` | `development` enables `/docs` |
| `LOG_LEVEL` | No | `INFO` | `DEBUG / INFO / WARNING / ERROR` |
| `APP_PORT` | No | `8000` | Server port |
| `ZAPIER_MAX_RETRIES` | No | `3` | Number of Zapier retry attempts |
| `ZAPIER_RETRY_BASE_DELAY_SECONDS` | No | `1.0` | Backoff base (doubles each retry) |
| `IDEMPOTENCY_CACHE_MAX_SIZE` | No | `10000` | Max message IDs tracked for dedup |
| `IDEMPOTENCY_CACHE_TTL_SECONDS` | No | `86400` | How long (seconds) to remember a message ID |

---

## 12. Security Notes

- ✅ HTTPS-only (enforced by env var validator)
- ✅ No hardcoded credentials anywhere in code
- ✅ All secrets in `.env` (gitignored)
- ✅ Timing-attack-safe token comparison (`hmac.compare_digest`)
- ✅ Non-root Docker user
- ✅ `/docs` disabled in production
