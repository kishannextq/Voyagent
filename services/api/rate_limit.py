"""Fixed-window rate limits per client (SECURITY.md F7 follow-up).

Backends:
- Redis (shared across replicas): one counter per (scope, client, window), expiring with the window.
- In-memory: per process, for local dev and tests only.

On a Redis error the limiter fails open (logs a warning): a cache outage should not take the
API down; MAX_CONCURRENT_RUNS still bounds load per process.

Clients are identified by IP until authentication lands; then by user/tenant.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("voyagent.rate_limit")


@dataclass(frozen=True)
class Limits:
    enabled: bool
    requests_per_minute: int  # any API call except health
    runs_per_minute: int  # calls that start or resume a graph run (LLM/tool cost)

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(
            enabled=os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            requests_per_minute=int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "120")),
            runs_per_minute=int(os.getenv("RATE_LIMIT_RUNS_PER_MINUTE", "10")),
        )


class Limiter(Protocol):
    async def hit(self, key: str, limit: int, window: int = 60) -> int | None:
        """Count one request. Returns None if allowed, else seconds until the window resets."""
        ...


def _retry_after(now: float, window: int) -> int:
    return max(1, int(window - (now % window)))


class MemoryLimiter:
    def __init__(self, clock=time.time, max_keys: int = 100_000):
        self._clock = clock
        self._max = max_keys
        self._counts: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

    async def hit(self, key: str, limit: int, window: int = 60) -> int | None:
        now = self._clock()
        bucket = int(now // window)
        with self._lock:
            if len(self._counts) >= self._max:
                # Drop counters from earlier windows; if still full, start over (bounded memory).
                self._counts = {k: v for k, v in self._counts.items() if k[1] == bucket}
                if len(self._counts) >= self._max:
                    self._counts.clear()
            count = self._counts.get((key, bucket), 0) + 1
            self._counts[(key, bucket)] = count
        return _retry_after(now, window) if count > limit else None


class RedisLimiter:
    PREFIX = "api:rl:"

    def __init__(self, client, clock=time.time):
        self._r = client
        self._clock = clock

    async def hit(self, key: str, limit: int, window: int = 60) -> int | None:
        now = self._clock()
        redis_key = f"{self.PREFIX}{key}:{int(now // window)}"
        try:
            async with self._r.pipeline(transaction=True) as pipe:
                pipe.incr(redis_key)
                pipe.expire(redis_key, window + 1)
                count, _ = await pipe.execute()
        except Exception as e:  # fail open, see module docstring
            log.warning("rate_limit_unavailable", extra={"error_type": type(e).__name__})
            return None
        return _retry_after(now, window) if count > limit else None


def build(redis_client) -> Limiter:
    return RedisLimiter(redis_client) if redis_client is not None else MemoryLimiter()
