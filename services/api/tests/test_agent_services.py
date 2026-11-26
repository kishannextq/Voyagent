"""Agent services stay consistent and least-privilege across compose, ElasticMQ and Terraform.

For every agent enabled as its own service:
- the orchestrator lists it in REMOTE_AGENTS, it has a worker service, a local queue with a
  dead-letter queue, and a Terraform entry (agent_bus.REMOTABLE only);
- it gets exactly the credentials its tools need (from agents/<name>.md), nothing more;
- its Terraform timeout matches its agent config, so queue visibility covers a full run.
"""

import pathlib
import re

import yaml

import agent_bus
import agent_config

ROOT = pathlib.Path(__file__).resolve().parents[3]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.agents.yml").read_text(encoding="utf-8"))
ELASTICMQ = (ROOT / "infra/elasticmq/elasticmq.conf").read_text(encoding="utf-8")
TF = (ROOT / "infra/terraform/envs/agents.tf").read_text(encoding="utf-8")

TOOL_KEYS = {
    "aviationstack": "AVIATION_STACK_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "openweather": "OPENWEATHER_API_KEY",
}
PROVIDER_KEYS = {*TOOL_KEYS.values(), "GROQ_API_KEY", "LLM_GATEWAY_KEY"}


def _remote() -> set[str]:
    raw = COMPOSE["services"]["orchestrator"]["environment"]["REMOTE_AGENTS"]
    return {a for a in raw.split(",") if a}


def _workers() -> dict[str, dict]:
    out = {}
    for name, svc in COMPOSE["services"].items():
        if name.startswith("agent-"):
            cmd = svc["command"]
            assert cmd[:2] == ["python", "agent_worker.py"], name
            out[cmd[2]] = svc
    return out


def _tf_agents() -> dict[str, dict]:
    out = {}
    for m in re.finditer(r"^\s{4}(\w+) = \{(.*?)^\s{4}\}", TF, re.S | re.M):
        body = m.group(2)
        secrets = re.search(r"secrets\s*=\s*\[(.*?)\]", body, re.S).group(1)
        out[m.group(1)] = {
            "timeout": float(re.search(r"timeout_seconds\s*=\s*([\d.]+)", body).group(1)),
            "secrets": set(re.findall(r'"(\w+)"', secrets)),
            "database": re.search(r"database\s*=\s*(true|false)", body).group(1) == "true",
        }
    return out


def _needs(agent: str) -> tuple[set[str], bool, bool]:
    """(provider secrets, needs database, needs guard) from the agent's config."""
    spec = agent_config.SPECS[agent]
    services = {t.split(".")[0] for t in spec.tools}
    secrets = {TOOL_KEYS[s] for s in services if s in TOOL_KEYS}
    if spec.max_llm_calls > 0 or "rag" in services:  # model calls; document search embeds via the gateway
        secrets.add("LLM_GATEWAY_KEY")
    return secrets, "rag" in services, bool(services & set(TOOL_KEYS))


def test_every_enabled_agent_is_wired_everywhere():
    remote, workers, tf = _remote(), _workers(), _tf_agents()
    assert remote, "no agent enabled"
    assert remote <= set(agent_bus.REMOTABLE)
    assert remote == set(workers) == set(tf), (remote, set(workers), set(tf))
    for agent in remote:
        q = agent_bus.queue_name(agent)
        assert re.search(rf"^\s*{q} \{{", ELASTICMQ, re.M), f"no local queue {q}"
        assert f'name = "{q}-dlq"' in ELASTICMQ, f"no dead-letter queue for {q}"


def test_least_privilege_credentials():
    tf = _tf_agents()
    for agent, svc in _workers().items():
        secrets, database, guard = _needs(agent)
        # Terraform: exactly what the tools need (+ the guard token for tool-calling agents).
        assert tf[agent]["secrets"] == secrets | ({"GUARD_TOKEN"} if guard else set()), agent
        assert tf[agent]["database"] == database, agent
        # Compose: no provider key beyond those (GROQ_API_KEY stands in for the gateway locally).
        env = set(svc["environment"])
        allowed = secrets | ({"GROQ_API_KEY"} if "LLM_GATEWAY_KEY" in secrets else set())
        assert env & PROVIDER_KEYS <= allowed, (agent, env & PROVIDER_KEYS - allowed)
        assert ("DATABASE_URL" in env) == database, agent


def test_timeouts_match_agent_config():
    for agent, cfg in _tf_agents().items():
        assert cfg["timeout"] == agent_config.SPECS[agent].timeout_seconds, agent
        m = re.search(rf"{agent_bus.queue_name(agent)} \{{\s*defaultVisibilityTimeout = (\d+) seconds", ELASTICMQ)
        assert m and int(m.group(1)) >= cfg["timeout"] + agent_bus.queue_wait_seconds(), agent
