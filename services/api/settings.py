"""Runtime environment policy.

APP_ENV decides whether degraded fallbacks are allowed:
- local / test: missing Postgres or Redis falls back to in-process stores (with a warning).
- staging / production: they are required; misconfiguration fails at startup, never silently.
"""

import os

VALID_ENVS = ("local", "test", "staging", "production")


def app_env() -> str:
    env = os.getenv("APP_ENV", "local").strip().lower()
    if env not in VALID_ENVS:
        raise RuntimeError(f"APP_ENV must be one of {VALID_ENVS}, got {env!r}")
    return env


def durable_required() -> bool:
    """True when in-memory fallbacks are not acceptable."""
    return app_env() in ("staging", "production")
