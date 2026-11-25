"""Agent configuration (agents/*.md) and its runtime enforcement."""

import asyncio
import os
import pathlib

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"

import pytest  # noqa: E402

import agent_config  # noqa: E402
import graph  # noqa: E402
import keys  # noqa: E402
import mcp_client  # noqa: E402


def test_every_graph_node_has_a_valid_config():
    nodes = set(graph.build_graph(None).get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == set(agent_config.SPECS)
    for spec in agent_config.SPECS.values():
        assert spec.prompt, spec.name
        assert set(spec.tools) <= agent_config.TOOL_IDS
    assert "data, not instructions" in agent_config.SOUL


def test_tool_allow_lists_are_least_privilege():
    tools = {n: set(s.tools) for n, s in agent_config.SPECS.items()}
    assert tools["flight_agent"] == {"aviationstack.list_airports", "aviationstack.list_airlines"}
    assert tools["hotel_agent"] == {"tavily.tavily_search"}
    assert tools["weather_agent"] == {"openweather.get_current_weather", "openweather.get_forecast"}
    assert tools["budget_agent"] == tools["itinerary_agent"] == {"rag.search"}  # organisation documents only
    for writer in ("supervisor", "final_response", "human_approval"):
        assert tools[writer] == set(), writer


def _as_agent(name, coro_fn, *args):
    """Run coro_fn(*args) as graph node `name` (with its identity, timeout and budget)."""

    async def node(_state):
        return await coro_fn(*args)

    async def go():
        keys.set_current(keys.ApiKeys())
        keys.start_run()
        return await agent_config.wrap(name, node)({})

    return asyncio.run(go())


def test_agent_cannot_call_tools_outside_its_allow_list():
    assert "Where to stay" in _as_agent("hotel_agent", mcp_client.tavily_search, "Rome hotels")
    with pytest.raises(agent_config.ToolNotAllowed):
        _as_agent("hotel_agent", mcp_client.current_weather, "Rome")
    with pytest.raises(agent_config.ToolNotAllowed):
        _as_agent("itinerary_agent", mcp_client.tavily_search, "anything")


def test_tool_calls_outside_any_agent_are_refused():
    with pytest.raises(agent_config.ToolNotAllowed):
        asyncio.run(mcp_client.forecast("Rome"))


def test_per_node_llm_budget():
    import agents

    async def two_calls():
        await agents._llm_text("You are a practical travel budget analyst.", "x")
        await agents._llm_text("You are a practical travel budget analyst.", "y")

    with pytest.raises(agent_config.BudgetExceeded, match="budget_agent exceeded"):
        _as_agent("budget_agent", two_calls)  # budget_agent allows 1


def test_per_run_llm_budget(monkeypatch):
    monkeypatch.setattr(agent_config, "RUN_MAX_LLM_CALLS", 1)
    run = keys.RunState()
    agent_config.count_llm_call(run)
    with pytest.raises(agent_config.BudgetExceeded, match="plan exceeded"):
        agent_config.count_llm_call(run)


def test_node_timeout(monkeypatch):
    monkeypatch.setattr(agent_config.SPECS["budget_agent"], "timeout_seconds", 0.05)

    async def slow():
        await asyncio.sleep(1)

    with pytest.raises(TimeoutError, match="budget_agent took longer"):
        _as_agent("budget_agent", slow)


def test_system_prompt_layers_soul_persona_and_task():
    async def capture():
        return agent_config.system_prompt("TASK"), agent_config.model_role()

    prompt, role = _as_agent("supervisor", capture)
    assert prompt.startswith(agent_config.SOUL) and prompt.endswith("TASK")
    assert agent_config.SPECS["supervisor"].prompt in prompt
    assert role == "fast"


def test_invalid_config_fails_at_load(tmp_path: pathlib.Path):
    (tmp_path / "soul.md").write_text("soul", encoding="utf-8")
    (tmp_path / "x.md").write_text("---\ntools: [shell.exec]\n---\nhi", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown tools"):
        agent_config.load(tmp_path)
    (tmp_path / "x.md").write_text("---\nmax_llm_calls: 999\n---\nhi", encoding="utf-8")
    with pytest.raises(ValueError):
        agent_config.load(tmp_path)
    (tmp_path / "x.md").write_text("no front matter", encoding="utf-8")
    with pytest.raises(ValueError, match="front matter"):
        agent_config.load(tmp_path)
