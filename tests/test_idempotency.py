"""
tests/test_idempotency.py

Unit tests for the IdempotencyCache — deduplication logic.
"""
from __future__ import annotations

import time
import pytest

from app.core.idempotency import IdempotencyCache


def test_first_message_not_duplicate():
    cache = IdempotencyCache(maxsize=100, ttl=60)
    assert cache.is_duplicate("msg-001") is False


def test_after_mark_seen_is_duplicate():
    cache = IdempotencyCache(maxsize=100, ttl=60)
    cache.mark_seen("msg-001")
    assert cache.is_duplicate("msg-001") is True


def test_different_ids_are_not_duplicates():
    cache = IdempotencyCache(maxsize=100, ttl=60)
    cache.mark_seen("msg-001")
    assert cache.is_duplicate("msg-002") is False


def test_cache_size_grows_with_entries():
    cache = IdempotencyCache(maxsize=100, ttl=60)
    for i in range(5):
        cache.mark_seen(f"msg-{i:03d}")
    assert cache.size == 5


def test_ttl_expiry_removes_entry():
    """Use a 1-second TTL and verify the entry expires."""
    cache = IdempotencyCache(maxsize=100, ttl=1)
    cache.mark_seen("msg-expire-me")
    assert cache.is_duplicate("msg-expire-me") is True

    time.sleep(1.5)  # Wait for TTL to expire
    assert cache.is_duplicate("msg-expire-me") is False
