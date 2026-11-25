"""Apply database migrations: application schema (Alembic) + LangGraph checkpointer tables.

Run once per deploy as a one-off task before the new service version starts:
    DATABASE_URL=... python migrate.py
Services then run with CHECKPOINTER_SETUP=false.
"""

import asyncio
import os
import pathlib
import sys

from alembic import command
from alembic.config import Config

import config  # noqa: F401 - builds DATABASE_URL from DB_* parts when needed

HERE = pathlib.Path(__file__).resolve().parent


def upgrade_app_schema(database_url: str) -> None:
    cfg = Config(str(HERE / "alembic.ini"))
    cfg.set_main_option("script_location", str(HERE / "migrations"))
    cfg.set_main_option("version_locations", str(HERE / "migrations" / "versions"))
    cfg.attributes["database_url"] = database_url
    command.upgrade(cfg, "head")


async def setup_checkpointer(database_url: str) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        await saver.setup()


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    upgrade_app_schema(url)
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(setup_checkpointer(url), loop_factory=factory)
    print("migrations applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
