import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import agent_config
import settings
from agents import (
    budget_agent,
    final_response_agent,
    flight_agent,
    hotel_agent,
    human_approval_agent,
    itinerary_agent,
    supervisor_agent,
    weather_agent,
)
from config import DATABASE_URL
from state import TravelState

log = logging.getLogger("voyagent.graph")

AGENT_ORDER = [
    "flight_agent",
    "hotel_agent",
    "weather_agent",
    "budget_agent",
    "itinerary_agent",
]

ROUTE_MAP = {
    "flight_agent": "flight_agent",
    "hotel_agent": "hotel_agent",
    "weather_agent": "weather_agent",
    "budget_agent": "budget_agent",
    "itinerary_agent": "itinerary_agent",
}

SUPERVISOR_ROUTE_MAP = {**ROUTE_MAP, "end": END}


def _selected_agents(state: TravelState) -> list[str]:
    selected = state.get("selected_agents")
    return [agent for agent in AGENT_ORDER if agent in (selected or [])]


def route_from_supervisor(state: TravelState) -> str:
    selected = _selected_agents(state)
    if not selected and state.get("final_response"):
        # Guardrail blocked the request; nothing to plan.
        return "end"
    return selected[0] if selected else "itinerary_agent"


def route_after_agent(current_agent: str):
    def route(state: TravelState) -> str:
        selected = _selected_agents(state)
        current_index = AGENT_ORDER.index(current_agent)

        for next_agent in AGENT_ORDER[current_index + 1 :]:
            if next_agent in selected:
                return next_agent

        return "itinerary_agent"

    return route


def build_graph(checkpointer):
    graph = StateGraph(TravelState)

    graph.add_node("supervisor", agent_config.wrap("supervisor", supervisor_agent))
    graph.add_node("flight_agent", agent_config.wrap("flight_agent", flight_agent))
    graph.add_node("hotel_agent", agent_config.wrap("hotel_agent", hotel_agent))
    graph.add_node("weather_agent", agent_config.wrap("weather_agent", weather_agent))
    graph.add_node("budget_agent", agent_config.wrap("budget_agent", budget_agent))
    graph.add_node("itinerary_agent", agent_config.wrap("itinerary_agent", itinerary_agent))
    graph.add_node("human_approval", agent_config.wrap("human_approval", human_approval_agent))
    graph.add_node("final_response", agent_config.wrap("final_response", final_response_agent))

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", route_from_supervisor, SUPERVISOR_ROUTE_MAP)
    graph.add_conditional_edges("flight_agent", route_after_agent("flight_agent"), ROUTE_MAP)
    graph.add_conditional_edges("hotel_agent", route_after_agent("hotel_agent"), ROUTE_MAP)
    graph.add_conditional_edges("weather_agent", route_after_agent("weather_agent"), ROUTE_MAP)
    graph.add_conditional_edges("budget_agent", route_after_agent("budget_agent"), ROUTE_MAP)
    graph.add_edge("itinerary_agent", "human_approval")
    graph.add_edge("human_approval", "final_response")
    graph.add_edge("final_response", END)

    return graph.compile(checkpointer=checkpointer)


# Upper bound on node executions per run; the longest real path is ~9 nodes.
RECURSION_LIMIT = 25


async def _noop() -> None:
    return None


async def open_checkpointer(database_url: str | None) -> tuple[Any, Callable[[], Awaitable[None]]]:
    """Checkpointer for LangGraph state (interrupt() needs one either way) and its close function.

    - DATABASE_URL set: AsyncPostgresSaver on an async connection pool. If the database is
      unreachable this raises: a configured database is never silently replaced by memory
      (SECURITY.md F10).
    - DATABASE_URL unset: in-memory, allowed only in local/test (APP_ENV).

    Must be called inside the running event loop that will use it (app lifespan).
    Note: psycopg async does not support Windows' default ProactorEventLoop; run the API
    with Postgres on Linux/containers (or a SelectorEventLoop) locally.
    """
    if database_url:
        timeout = float(os.getenv("PG_POOL_TIMEOUT", "10"))
        pool = AsyncConnectionPool(
            database_url,
            min_size=int(os.getenv("PG_POOL_MIN", "1")),
            max_size=int(os.getenv("PG_POOL_MAX", "10")),
            timeout=timeout,
            # Required by the Postgres saver: autocommit (setup() runs CREATE INDEX CONCURRENTLY),
            # no server-side prepared statements (poolers), rows as dicts.
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row, "connect_timeout": 5},
            open=False,
            name="checkpointer",
        )
        try:
            await pool.open(wait=True, timeout=timeout)
            saver = AsyncPostgresSaver(pool)
            # Library-managed tables. In deployed envs run this once as a migration task and set
            # CHECKPOINTER_SETUP=false on the service (docs/DATABASE_DESIGN.md).
            if os.getenv("CHECKPOINTER_SETUP", "true").lower() in {"1", "true", "yes"}:
                await saver.setup()
        except BaseException:
            await pool.close()
            raise
        log.info("checkpointer_ready", extra={"backend": "postgres", "pool_max": pool.max_size})
        return saver, pool.close
    if settings.durable_required():
        raise RuntimeError(f"DATABASE_URL is required when APP_ENV={settings.app_env()}")
    log.warning("checkpointer_ready", extra={"backend": "memory", "note": "local/test only"})
    return MemorySaver(), _noop


async def open_graph(database_url: str | None = DATABASE_URL):
    """Compiled graph plus the close function for its checkpointer."""
    saver, close = await open_checkpointer(database_url)
    return build_graph(saver), close
