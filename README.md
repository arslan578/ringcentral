# RC SMS Webhook Integration

Real-time RingCentral SMS to Zapier webhook router. Captures **all** inbound and outbound SMS messages across every RC user/extension, enriches them with full metadata from the RC REST API, and forwards them to a Zapier webhook endpoint for automated DNC (Do Not Contact) language detection.

## Architecture

```
RingCentral  ──webhook push──>  This Service  ──HTTPS POST──>  Zapier Webhook
(change notification)           (fetch full     (full metadata
 with message IDs)               message via     JSON payload)
                                 RC REST API)
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Service info and endpoint directory |
| `GET` | `/api/v1/health` | Health check (used by Docker, load balancers, uptime monitors) |
| `GET` | `/api/v1/rc/webhook?validationToken=...` | RC webhook validation challenge (echoes token) |
| `POST` | `/api/v1/rc/webhook` | Main receiver - processes RC SMS notifications and forwards to Zapier |
| `GET` | `/docs` | Swagger UI (development mode only) |
| `GET` | `/redoc` | ReDoc API docs (development mode only) |

## Zapier Payload Fields

Every SMS forwarded to Zapier includes these **flat, individually-mappable** fields:

| Field | Description |
|-------|-------------|
| `source` | Always `"ringcentral"` |
| `event_type` | `"inbound_sms"` or `"outbound_sms"` |
| `message_id` | RC unique message ID |
| `message_type` | Message type (e.g. `"SMS"`) |
| `direction` | `"Inbound"` or `"Outbound"` |
| `from_number` | Sender phone number (E.164) |
| `from_name` | Sender name (if available) |
| `from_location` | Sender location (if available) |
| `to_number` | Recipient phone number (E.164) |
| `to_name` | Recipient name (if available) |
| `to_location` | Recipient location (if available) |
| `subject` | SMS subject (same as body for SMS) |
| `body` | SMS message text content |
| `timestamp_utc` | Message creation time (ISO-8601 UTC) |
| `last_modified_utc` | Last modified time (ISO-8601 UTC) |
| `sms_delivery_time_utc` | SMS delivery time (ISO-8601 UTC) |
| `received_at_utc` | Server processing time (ISO-8601 UTC) |
| `account_id` | RC account ID |
| `extension_id` | RC extension ID |
| `subscription_id` | RC webhook subscription ID |
| `conversation_id` | RC conversation/thread ID |
| `read_status` | e.g. `"Unread"`, `"Read"` |
| `message_status` | e.g. `"Received"`, `"Sent"` |
| `delivery_error_code` | Carrier error code (if any) |
| `priority` | e.g. `"Normal"` |
| `availability` | e.g. `"Alive"` |
| `attachment_count` | Number of MMS attachments |
| `message_uri` | RC API URI for this message |
| `rc_event_type` | RC event URI path |
| `rc_event_uuid` | RC notification UUID |

## Quick Start

### 1. Clone and install

```bash
git clone <repo-url>
cd ringcentral
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in your values:

```env
RC_WEBHOOK_VERIFICATION_TOKEN=your_rc_verification_token
RC_CLIENT_ID=your_rc_client_id
RC_CLIENT_SECRET=your_rc_client_secret
RC_JWT_TOKEN=your_rc_jwt_token
RC_SERVER_URL=https://platform.ringcentral.com
RC_WEBHOOK_DELIVERY_URL=https://your-app.ondigitalocean.app/api/v1/rc/webhook
ZAPIER_WEBHOOK_URL=https://hooks.zapier.com/hooks/catch/...
APP_ENV=development
LOG_LEVEL=INFO
```

### 3. Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 4. Run with Docker

```bash
docker-compose up --build
```

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

## Project Structure

```
app/
  main.py                  # FastAPI app factory + lifespan
  config.py                # Pydantic settings from .env
  api/v1/
    endpoints/
      health.py            # GET /api/v1/health
      rc_webhook.py        # GET+POST /api/v1/rc/webhook
    router.py              # v1 router aggregation
  schemas/
    rc_message.py          # RC webhook + message models
    zapier_payload.py      # Zapier payload schema + factory
  services/
    rc_api_client.py       # RC REST API client (JWT auth)
    zapier_forwarder.py    # Zapier HTTP forwarder with retries
  core/
    idempotency.py         # TTL-based dedup cache
    logging.py             # Structured logging setup
    rc_validator.py        # Verification-Token validator
    exceptions.py          # Custom exception classes
tests/                     # pytest test suite
scripts/                   # Utility scripts (auth testing, subscription creation)
```

## Key Features

- **Full metadata extraction** - Fetches complete message data from RC REST API (webhooks only send IDs)
- **Inbound + Outbound SMS** - Processes both directions
- **Retry with exponential backoff** - 3 retries on Zapier failures (1s, 2s, 4s)
- **Idempotency** - TTL-based in-memory cache prevents duplicate forwarding
- **JWT authentication** - Server-to-server auth with automatic token refresh
- **Structured logging** - JSON file logs + human-readable console output
- **Auto subscription management** - Creates and auto-renews RC webhook subscription (no manual scripts needed)
- **Docker ready** - Multi-stage Dockerfile with health checks
