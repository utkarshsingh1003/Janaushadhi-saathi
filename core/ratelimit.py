"""Small in-process rate limiter.

This is intentionally dependency-free. It is per worker/process, so it is a
best-effort abuse control on a single small instance, not a distributed quota.
A shared store would be required for strict multi-instance limits.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            cutoff = now - window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(window_seconds - (now - bucket[0])))
                return False, retry_after
            bucket.append(now)
            if len(self._hits) > 5000:
                # Keep memory bounded on a long-running process.
                for stale_key in [k for k, v in self._hits.items() if not v][:1000]:
                    self._hits.pop(stale_key, None)
            return True, 0
