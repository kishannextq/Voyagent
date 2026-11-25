"""One shared async Redis client per process (run metadata, rate limits).

- REDIS_URL set: connect and ping at startup; a bad URL fails startup.
- REDIS_URL unset: None (callers use in-memory fallbacks), allowed only in local/test.
"""

import logging
import os

import settings

log = logging.getLogger("voyagent.redis")


async def connect():
    settings.app_env()  # validates APP_ENV
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        if settings.durable_required():
            raise RuntimeError(f"REDIS_URL is required when APP_ENV={settings.app_env()}")
        log.warning("redis_disabled", extra={"note": "in-memory fallbacks, local/test only"})
        return None

    import redis.asyncio as redis

    client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2, health_check_interval=30)
    try:
        await client.ping()  # fail fast on misconfiguration
    except BaseException:
        await client.aclose()
        raise
    log.info("redis_ready")
    return client
