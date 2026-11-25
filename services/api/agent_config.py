"""Per-agent configuration and runtime enforcement.

agents/soul.md      shared principles, prepended to every agent's system prompt
agents/<name>.md    one file per graph node: YAML front matter + persona/instructions body

Front matter (validated at startup; a bad file stops the service):
    model: fast | smart            gateway alias role
    tools: [service.tool, ...]     allow-list; any other tool call is refused
    max_llm_calls: int             per node execution
    timeout_seconds: number        per node execution

Enforced here: tool allow-list (check_tool), per-node timeout and LLM-call budget (wrap),
and a per-run LLM-call budget across all nodes (RUN_MAX_LLM_CALLS).
"""

import asyncio
import functools
import logging
import os
import pathlib
import re
from contextvars import ContextVar
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

import metrics
import telemetry

log = logging.getLogger("voyagent.agent_config")

AGENTS_DIR = pathlib.Path(os.getenv("AGENTS_DIR", pathlib.Path(__file__).parent / "agents"))
RUN_MAX_LLM_CALLS = int(os.getenv("RUN_MAX_LLM_CALLS", "20"))
TOOL_IDS = {
    "aviationstack.list_airports",
    "aviationstack.list_airlines",
    "tavily.tavily_search",
    "openweather.get_current_weather",
    "openweather.get_forecast",
    "rag.search",
}


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    model: Literal["fast", "smart"] = "smart"
    tools: list[str] = Field(default_factory=list)
    max_llm_calls: int = Field(2, ge=0, le=10)
    timeout_seconds: float = Field(60, gt=0, le=600)
    prompt: str = ""


class BudgetExceeded(Exception):
    pass


class ToolNotAllowed(Exception):
    pass


_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def _parse(path: pathlib.Path) -> AgentSpec:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = _FRONT.match(text)
    if not m:
        raise ValueError(f"{path.name}: missing YAML front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    spec = AgentSpec(name=path.stem, prompt=m.group(2).strip(), **meta)
    unknown = set(spec.tools) - TOOL_IDS
    if unknown:
        raise ValueError(f"{path.name}: unknown tools {sorted(unknown)}")
    return spec


def load(directory: pathlib.Path = AGENTS_DIR) -> tuple[str, dict[str, AgentSpec]]:
    soul = (directory / "soul.md").read_text(encoding="utf-8").strip()
    specs = {p.stem: _parse(p) for p in sorted(directory.glob("*.md")) if p.stem != "soul"}
    return soul, specs


SOUL, SPECS = load()


# ---- runtime ----

_current_agent: ContextVar[str] = ContextVar("current_agent", default="")
_node_calls: ContextVar[list[int] | None] = ContextVar("node_llm_calls", default=None)


def current() -> AgentSpec | None:
    return SPECS.get(_current_agent.get())


def system_prompt(task_system: str) -> str:
    """Soul + this agent's persona + the call's task-specific instruction."""
    spec = current()
    parts = [SOUL, spec.prompt if spec else "", task_system]
    return "\n\n".join(p for p in parts if p)


def model_role(default: str = "smart") -> str:
    spec = current()
    return spec.model if spec else default


def check_tool(service: str, tool_name: str) -> None:
    spec = current()
    tool_id = f"{service}.{tool_name}"
    if spec is None or tool_id not in spec.tools:
        log.warning("tool_not_allowed", extra={"agent": _current_agent.get() or None, "tool": tool_id})
        raise ToolNotAllowed(f"{tool_id} is not allowed for agent {_current_agent.get() or '(none)'}")


def count_llm_call(run_state) -> None:
    """Called before every model call. Raises BudgetExceeded past the node or run budget."""
    spec = current()
    calls = _node_calls.get()
    if spec is not None and calls is not None:
        calls[0] += 1
        if calls[0] > spec.max_llm_calls:
            raise BudgetExceeded(f"{spec.name} exceeded its budget of {spec.max_llm_calls} model calls")
    run_state.llm_calls = getattr(run_state, "llm_calls", 0) + 1
    if run_state.llm_calls > RUN_MAX_LLM_CALLS:
        raise BudgetExceeded(f"plan exceeded its budget of {RUN_MAX_LLM_CALLS} model calls")


def wrap(name: str, node):
    """Run a graph node with its agent identity, timeout and per-node LLM budget."""
    spec = SPECS.get(name)
    if spec is None:
        raise ValueError(f"no agents/{name}.md for graph node {name}")

    @functools.wraps(node)
    async def run(state):
        _current_agent.set(name)  # node tasks run in their own context copy
        _node_calls.set([0])
        start = asyncio.get_running_loop().time()
        try:
            with telemetry.tracer.start_as_current_span(f"agent {name}", attributes={"agent.name": name}):
                return await asyncio.wait_for(node(state), spec.timeout_seconds)
        except BudgetExceeded:
            metrics.budget_exceeded.labels("node_or_run").inc()
            raise
        except TimeoutError:
            log.warning("agent_timeout", extra={"agent": name, "timeout_s": spec.timeout_seconds})
            raise TimeoutError(f"{name} took longer than {spec.timeout_seconds:.0f}s") from None
        finally:
            metrics.agent_duration.labels(name).observe(asyncio.get_running_loop().time() - start)

    return run
