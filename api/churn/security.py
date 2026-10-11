"""Small security helpers: rate limiting, CSV-injection protection, response headers."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
}


def csv_safe(value):
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start of a text cell)."""
    if isinstance(value, str) and value and value[0] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return '' if value is None else value


class RateLimiter:
    """In-process sliding window. Fine for one instance; use a shared store (Redis) behind a load balancer."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(window - (now - q[0])))
            q.append(now)
            if len(self._hits) > 20000:      # bound memory
                for k in [k for k, v in self._hits.items() if not v][:5000]:
                    self._hits.pop(k, None)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()
