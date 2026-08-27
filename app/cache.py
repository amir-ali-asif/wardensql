"""In-process TTL cache.

Repeat questions are common (demos, dashboards, retries). Caching the full answer
lets an identical question return instantly with zero LLM calls -- the single
biggest lever on free-tier token budget. The interface is deliberately small so a
Redis-backed implementation can drop in for multi-instance deployments.
"""

from __future__ import annotations

import hashlib
import time
from threading import Lock
from typing import Any


def make_key(*parts: str) -> str:
    return hashlib.sha256("\u0000".join(parts).encode()).hexdigest()


class TTLCache:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expires_at, value = item
            if now >= expires_at:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
