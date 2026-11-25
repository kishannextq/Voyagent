"""Alembic environment: DATABASE_URL from the environment, psycopg 3 driver, version table in `app`."""

import os

from alembic import context
from sqlalchemy import create_engine, text


def _url() -> str:
    url = context.config.attributes.get("database_url") or os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is required to run migrations")
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def run_migrations_online() -> None:
    engine = create_engine(_url(), connect_args={"connect_timeout": 5})
    with engine.connect() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS app"))
        conn.commit()
        context.configure(connection=conn, version_table_schema="app", transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported; run against a database")
run_migrations_online()
