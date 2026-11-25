import logging
import os

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

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

    graph.add_node("supervisor", supervisor_agent)
    graph.add_node("flight_agent", flight_agent)
    graph.add_node("hotel_agent", hotel_agent)
    graph.add_node("weather_agent", weather_agent)
    graph.add_node("budget_agent", budget_agent)
    graph.add_node("itinerary_agent", itinerary_agent)
    graph.add_node("human_approval", human_approval_agent)
    graph.add_node("final_response", final_response_agent)

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


def build_checkpointer(database_url: str | None):
    """Checkpointer for LangGraph state (interrupt() needs one either way).

    - DATABASE_URL set: pooled PostgresSaver. If the database is unreachable this raises:
      a configured database is never silently replaced by memory (SECURITY.md F10).
    - DATABASE_URL unset: in-memory, allowed only in local/test (APP_ENV).
    """
    if database_url:
        pool = ConnectionPool(
            database_url,
            min_size=int(os.getenv("PG_POOL_MIN", "1")),
            max_size=int(os.getenv("PG_POOL_MAX", "10")),
            timeout=float(os.getenv("PG_POOL_TIMEOUT", "10")),
            # Required by PostgresSaver: autocommit (setup() runs CREATE INDEX CONCURRENTLY),
            # no server-side prepared statements (poolers), rows as dicts.
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row, "connect_timeout": 5},
            open=False,
            name="checkpointer",
        )
        pool.open(wait=True, timeout=float(os.getenv("PG_POOL_TIMEOUT", "10")))
        saver = PostgresSaver(pool)
        # Library-managed tables. In deployed envs run this once as a migration task and set
        # CHECKPOINTER_SETUP=false on the service (docs/DATABASE_DESIGN.md).
        if os.getenv("CHECKPOINTER_SETUP", "true").lower() in {"1", "true", "yes"}:
            saver.setup()
        log.info("checkpointer_ready", extra={"backend": "postgres", "pool_max": pool.max_size})
        return saver
    if settings.durable_required():
        raise RuntimeError(f"DATABASE_URL is required when APP_ENV={settings.app_env()}")
    log.warning("checkpointer_ready", extra={"backend": "memory", "note": "local/test only"})
    return MemorySaver()


def build_graph_with_checkpointer():
    return build_graph(build_checkpointer(DATABASE_URL))


app = build_graph_with_checkpointer()
