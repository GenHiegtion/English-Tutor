"""In-process sliding-window rate limits (the service runs as a single instance)."""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

PRUNE_EVERY = 1000  # drop idle keys after this many hits, so memory stays bounded


class SlidingWindowLimiter:
    """At most ``limit`` hits per key within any ``window_s`` seconds."""

    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._since_prune = 0

    def hit(self, key: str) -> int | None:
        """Record a hit; when over the limit, record nothing and return seconds to wait."""
        now = self._clock()
        self._maybe_prune(now)
        hits = self._hits.setdefault(key, deque())
        self._expire(hits, now)
        if len(hits) >= self._limit:
            return max(1, math.ceil(hits[0] + self._window_s - now))
        hits.append(now)
        return None

    def _expire(self, hits: deque[float], now: float) -> None:
        while hits and hits[0] <= now - self._window_s:
            hits.popleft()

    def _maybe_prune(self, now: float) -> None:
        self._since_prune += 1
        if self._since_prune < PRUNE_EVERY:
            return
        self._since_prune = 0
        for key in list(self._hits):
            self._expire(self._hits[key], now)
            if not self._hits[key]:
                del self._hits[key]
