# ============================================================
# Dockerfile — RC SMS Webhook Integration
# Base: Python 3.12 slim (minimal attack surface)
# ============================================================
FROM python:3.12-slim AS base

# System hardening
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ──────────────────────────────────────────
# Stage: dependencies
# ──────────────────────────────────────────
FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ──────────────────────────────────────────
# Stage: final runtime image
# ──────────────────────────────────────────
FROM deps AS runtime

# Non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

# Copy application source
COPY app/ ./app/

# Create logs directory owned by appuser
RUN mkdir -p /app/logs && chown appuser:appgroup /app/logs

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')"

# IMPORTANT: Use exactly 1 worker.  Multiple workers each create their
# own RC webhook subscription and their own in-memory idempotency cache,
# causing every SMS to be forwarded to Zapier N times (once per worker).
# A single async worker handles high concurrency just fine via asyncio.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
