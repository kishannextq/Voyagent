"""Agents as separate services, checked on the real compose stack (CI `agents` job).

    docker compose -f docker-compose.yml -f docker-compose.agents.yml up -d --build
    python3 infra/resilience/agents_check.py

1. A plan runs end to end with every agent-* service doing its part.
2. Scale-out: each agent at 3 replicas, 12 concurrent plans; work spreads across replicas.
3. Queue durability: an agent service is stopped, a plan is started, the agent comes back
   and the queued task completes the plan.

Standard library only. Exits non-zero on any failed check.
"""

import concurrent.futures
import json
import os
import subprocess
import sys
import time
import urllib.request

API = os.getenv("API_URL", "http://127.0.0.1:8000")
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.agents.yml"]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}", flush=True)
    return ok


def compose(*args: str) -> str:
    return subprocess.run([*COMPOSE, *args], check=True, capture_output=True, text=True).stdout  # noqa: S603


def agent_services() -> list[str]:
    return sorted(s for s in compose("config", "--services").split() if s.startswith("agent-"))


def post(path: str, body: dict, timeout: float = 180) -> tuple[int, str]:
    req = urllib.request.Request(  # noqa: S310 - fixed local URL
        API + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.status, r.read().decode()


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        lines = block.split("\n")
        if lines and lines[0].startswith("event: "):
            data = next((ln[6:] for ln in lines if ln.startswith("data: ")), "{}")
            out.append((lines[0][7:], json.loads(data)))
    return out


def plan(query: str) -> tuple[bool, str]:
    """Plan -> review -> approve. Returns (ok, detail)."""
    try:
        status, text = post("/api/v1/plan/stream", {"query": query})
        ev = events(text)
        names = [e for e, _ in ev]
        if status != 200 or names[-1:] != ["done"] or "interrupt" not in names:
            errors = [d.get("message") for e, d in ev if e == "error"]
            return False, f"events={names} errors={errors}"
        tid = ev[0][1]["thread_id"]
        status, text = post(f"/api/v1/plan/{tid}/approve", {"approved": True})
        state = json.loads(text)
        if status != 200 or state.get("status") != "completed" or not state["state"].get("final_response"):
            return False, f"approve status={state.get('status')}"
        return True, tid
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def handled_by(service: str) -> dict[str, int]:
    """Tasks completed per container of a service, from its logs."""
    counts: dict[str, int] = {}
    for line in compose("logs", "--no-color", service).splitlines():
        if '"agent_task_done"' in line and '"ok": true' in line:
            container = line.split("|", 1)[0].strip()
            counts[container] = counts.get(container, 0) + 1
    return counts


def wait_healthy(services: list[str], timeout: float = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = compose("ps", "--format", "json", *services).strip()
        ps = json.loads(raw) if raw.startswith("[") else [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
        if ps and all(p.get("Health") in ("healthy", "") and p.get("State") == "running" for p in ps):
            return True
        time.sleep(3)
    return False


def main() -> int:
    agents = agent_services()
    if not agents:
        print("no agent-* services defined in docker-compose.agents.yml: nothing to check")
        return 0
    print(f"agent services: {', '.join(agents)}")

    # 1. end to end
    ok, detail = plan("5 day trip to Lisbon in May for two, mid-range budget")
    check("plan completes with agents as separate services", ok, detail)
    for svc in agents:
        n = sum(handled_by(svc).values())
        check(f"{svc} handled its task", n >= 1, f"{n} task(s)")

    # 2. scale out
    compose("up", "-d", "--no-recreate", *[a for svc in agents for a in ("--scale", f"{svc}=3")], *agents)
    check("agents scaled to 3 replicas", wait_healthy(agents), "")
    before = {svc: handled_by(svc) for svc in agents}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(plan, [f"{d} day trip to Kyoto, plan {i}" for i, d in enumerate([3, 4, 5] * 4)]))
    failed = [d for ok, d in outcomes if not ok]
    check("12 concurrent plans complete", not failed, f"{len(outcomes) - len(failed)}/12" + (f" first error: {failed[0]}" if failed else ""))
    for svc in agents:
        after = handled_by(svc)
        busy = [c for c, n in after.items() if n > before[svc].get(c, 0)]
        check(f"{svc} work spread across replicas", len(busy) >= 2, f"{len(busy)} of 3 replicas took tasks")

    # 3. queue durability: the task waits in SQS while the agent is down
    target = agents[-1]
    compose("stop", target)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(plan, "3 day trip to Oslo in June")
        time.sleep(5)
        compose("start", target)
        ok, detail = fut.result()
    check(f"plan completes after {target} comes back (task waited in its queue)", ok, detail)

    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
