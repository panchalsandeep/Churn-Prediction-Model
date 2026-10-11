"""Small security helpers: rate limiting, CSV-injection protection, response headers."""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque

log = logging.getLogger('churnsight.security')

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


class RedisRateLimiter:
    """Sliding window shared by every web process (REDIS_URL). If Redis is unreachable we fall back to the per-process
    limiter instead of failing requests: limits get looser, availability is kept."""

    def __init__(self, client, fallback: RateLimiter) -> None:
        self._r, self._fallback = client, fallback

    def allow(self, key: str, limit: int, window: float = 60.0) -> tuple[bool, int]:
        now, k = time.time(), f'rl:{key}'
        try:
            pipe = self._r.pipeline()
            pipe.zremrangebyscore(k, 0, now - window)
            pipe.zcard(k)
            pipe.zrange(k, 0, 0, withscores=True)
            _, count, oldest = pipe.execute()
            if count >= limit:
                return False, max(1, int(window - (now - oldest[0][1]))) if oldest else int(window)
            pipe = self._r.pipeline()
            pipe.zadd(k, {f'{now}:{uuid.uuid4().hex[:8]}': now})
            pipe.expire(k, int(window) + 1)
            pipe.execute()
            return True, 0
        except Exception as exc:  # noqa: BLE001  (any redis error)
            log.warning('rate limiter falling back to in-process counting: %s', exc)
            return self._fallback.allow(key, limit, window)

    def reset(self) -> None:
        self._fallback.reset()
        try:
            for k in self._r.scan_iter('rl:*'):
                self._r.delete(k)
        except Exception:  # noqa: BLE001
            pass


def make_limiter(url: str | None = None, client=None):
    local = RateLimiter()
    url = url if url is not None else os.environ.get('REDIS_URL')
    if client is None and url:
        import redis  # noqa: PLC0415 (optional dependency)
        client = redis.Redis.from_url(url, socket_timeout=0.25, socket_connect_timeout=0.25)
    return RedisRateLimiter(client, local) if client is not None else local


limiter = make_limiter()
