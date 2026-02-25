"""
app/core/idempotency.py

In-memory TTL-based deduplication cache.

Keyed on RC message ID (string). Prevents the same inbound SMS from being
forwarded to Zapier more than once within the TTL window (default 24h).

Thread-safe: cachetools.TTLCache is NOT inherently thread-safe, so we wrap
all access with a threading.Lock.

If you scale horizontally (multiple server instances), replace this with a
shared Redis-backed cache and use SETNX + EXPIRE.
"""
import threading
import logging
from cachetools import TTLCache

logger = logging.getLogger(__name__)


class IdempotencyCache:
    """
    Singleton-style wrapper around a TTLCache for message-ID deduplication.
    Injected into the FastAPI app state on startup.
    """

    def __init__(self, maxsize: int = 10_000, ttl: int = 86_400):
        self._cache: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = threading.Lock()
        logger.info(
            "Idempotency cache initialized",
            extra={"maxsize": maxsize, "ttl_seconds": ttl},
        )

    def is_duplicate(self, message_id: str) -> bool:
        """Return True if message_id was already processed within the TTL window."""
        with self._lock:
            return message_id in self._cache

    def mark_seen(self, message_id: str) -> None:
        """Record message_id as processed."""
        with self._lock:
            self._cache[message_id] = True
        logger.debug(
            "Message marked as processed",
            extra={"message_id": message_id},
        )

    @property
    def size(self) -> int:
        """Current number of tracked message IDs."""
        with self._lock:
            return len(self._cache)
