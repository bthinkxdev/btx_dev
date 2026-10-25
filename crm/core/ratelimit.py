"""Simple fixed-window rate limiting for public endpoints (Part 15.1). Uses the configured cache (Redis in production)."""
from __future__ import annotations

import time

from django.core.cache import cache


def hit(bucket: str, key: str, limit: int, window_seconds: int) -> bool:
    """Returns True if allowed, False if the limit is exceeded."""
    if not key:
        key = 'anon'
    slot = int(time.time() // window_seconds)
    cache_key = f'crm-rl:{bucket}:{key}:{slot}'
    try:
        added = cache.add(cache_key, 1, timeout=window_seconds + 5)
        if added:
            return True
        count = cache.incr(cache_key)
    except Exception:  # cache unavailable: fail open rather than block payments/leads
        return True
    return count <= limit
