"""Per-key token-bucket rate limiter (in-process).

Protects the service and your free-tier LLM budget from a single caller. For a
multi-instance deployment swap this for a Redis-backed bucket; the interface stays
the same.
"""

from __future__ import annotations

import time
from threading import Lock


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self._capacity = per_minute
        self._refill_per_sec = per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}   # key -> (tokens, last_ts)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill_per_sec)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True
