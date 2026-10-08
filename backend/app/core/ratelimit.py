"""Fixed-window rate limiter backed by Redis with an in-process fallback.

If Redis is unavailable the limiter degrades to a per-process counter instead of
failing open, so brute-force protection keeps working on a single instance.
"""

from __future__ import annotations

import logging
import threading
import time

import redis

from app.core.config import get_settings

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, redis_url: str | None = None, prefix: str = "permutados:rl:"):
        self.prefix = prefix
        self._redis = redis.Redis.from_url(
            redis_url or get_settings().redis_url, socket_timeout=0.5, socket_connect_timeout=0.5
        )
        self._local: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        """Register a hit; return True if allowed, False if the limit is exceeded."""
        bucket = int(time.time() // window_seconds)
        full = f"{self.prefix}{key}:{bucket}"
        try:
            pipe = self._redis.pipeline()
            pipe.incr(full)
            pipe.expire(full, window_seconds + 1)
            count, _ = pipe.execute()
            return int(count) <= limit
        except redis.RedisError:
            log.warning("rate limiter: redis unavailable, using local fallback")
            with self._lock:
                c, exp = self._local.get(full, (0, time.time() + window_seconds))
                c += 1
                self._local[full] = (c, exp)
                now = time.time()
                for k in [k for k, (_, e) in self._local.items() if e < now]:
                    self._local.pop(k, None)
                return c <= limit

    def reset(self, key_prefix: str = "") -> None:
        try:
            for k in self._redis.scan_iter(f"{self.prefix}{key_prefix}*"):
                self._redis.delete(k)
        except redis.RedisError:
            pass
        with self._lock:
            self._local.clear()


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    global _limiter
    _limiter = limiter
