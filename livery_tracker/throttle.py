"""Guards against hammering the free APIs we depend on.

Three small, thread-safe primitives:
  * TTLCache    — memoise an expensive fetch for a short window
  * MinInterval — enforce a floor on the gap between calls to one host
  * Cooldown    — per-user spacing for expensive bot commands

Background jobs are already paced (3s between tails at harvest, 120s live
polling, 15-min watch sweeps). These cover the paths a *person* can trigger
on demand, which otherwise have no ceiling at all.
"""

from __future__ import annotations

import threading
import time
from typing import Any

MISS = object()


class TTLCache:
    """Tiny in-process cache. Not persisted — it only absorbs bursts."""

    def __init__(self, ttl_seconds: float, max_entries: int = 256) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return MISS
            stored_at, value = entry
            if time.monotonic() - stored_at > self.ttl:
                del self._entries[key]
                return MISS
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            if len(self._entries) >= self.max_entries:
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                del self._entries[oldest]
            self._entries[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class MinInterval:
    """Blocks until at least `seconds` have passed since the previous call.

    Called from worker threads (asyncio.to_thread), so sleeping here never
    stalls the bot's event loop.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> float:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._last + self.seconds - now)
            if delay:
                time.sleep(delay)
            self._last = time.monotonic()
            return delay


class Cooldown:
    """Per-key command spacing. `remaining()` is 0 when the call may proceed."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._used: dict[Any, float] = {}
        self._lock = threading.Lock()

    def remaining(self, key: Any) -> float:
        with self._lock:
            last = self._used.get(key)
            now = time.monotonic()
            if last is not None and now - last < self.seconds:
                return self.seconds - (now - last)
            self._used[key] = now
            return 0.0

    def clear(self) -> None:
        with self._lock:
            self._used.clear()
