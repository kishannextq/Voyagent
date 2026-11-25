"""Failure-injection and backup/restore drill against the docker compose stack (run in CI).

Expects `docker compose up -d postgres redis migrate orchestrator api` to be running with
MOCK_MODE=true. Stdlib only. Each scenario prints PASS/FAIL; any FAIL exits non-zero.

1. Orchestrator dies mid-plan  -> the client stream ends promptly (no hang); the API stays
   healthy and answers 502 while it's down; after restart, state from before is still served.
2. Redis down                  -> plans still complete (run metadata degrades); health OK.
3. Postgres restart            -> pooled connections recover without failed requests afterwards.
4. Backup / restore            -> pg_dump, restore into a fresh database, and a paused plan from
                                  before the backup resumes to completion on the restored copy.
"""

import http.client
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8000"
RESULTS: list[tuple[str, bool, str]] = []


def compose(*args: str, check: bool = True) -> str:
    out = subprocess.run(["docker", "compose", *args], capture_output=True, text=True, check=False)
    if check and out.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {out.stderr[-500:]}")
    return out.stdout


def http(method: str, path: str, body=None, timeout: float = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - local URL
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
        return 0, str(e)


def wait_healthy(path="/api/v1/health", timeout=120) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if http("GET", path, timeout=5)[0] == 200:
            return True
        time.sleep(2)
    return False


def plan(query="3 days in Rome"):
    status, body = http("POST", "/api/v1/plan", {"query": query}, timeout=120)
    return status, (json.loads(body) if status == 200 else body)


def record(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name} {detail}", flush=True)


def scenario_orchestrator_crash():
    status, before = plan()
    assert status == 200, before
    tid = before["thread_id"]

    outcome = {}

    def stream():
        start = time.time()
        status, body = http("POST", "/api/v1/plan/stream", {"query": "5 days in Kyoto"}, timeout=90)
        outcome.update(status=status, body=body, seconds=time.time() - start)

    t = threading.Thread(target=stream)
    t.start()
    time.sleep(1.5)  # the run is in progress (slow mock delay)
    compose("kill", "orchestrator")
    t.join(timeout=100)
    record("stream ends after orchestrator crash", not t.is_alive() and outcome.get("seconds", 999) < 60,
           f"({outcome.get('seconds', 0):.1f}s, status {outcome.get('status')})")
    record("...with a clean error event (not a cut connection)", "event: error" in str(outcome.get("body", "")))
    record("api health while orchestrator is down", http("GET", "/api/v1/health", timeout=10)[0] in (200, 502))
    status, _ = plan()
    record("new plan while orchestrator is down fails fast with 502", status == 502, f"(got {status})")

    compose("up", "-d", "orchestrator")
    wait_healthy()
    end = time.time() + 120
    status = 0
    while time.time() < end:
        status, body = http("GET", f"/api/v1/plan/{tid}")
        if status == 200:
            break
        time.sleep(2)
    record("plan from before the crash is served after restart", status == 200 and json.loads(body)["status"] == "awaiting_approval")
    status, body = http("POST", f"/api/v1/plan/{tid}/approve", {"approved": True}, timeout=120)
    record("...and can be approved", status == 200 and json.loads(body)["status"] == "completed")


def scenario_redis_down():
    compose("stop", "redis")
    try:
        status, body = plan()
        record("plan completes with Redis down", status == 200 and body["status"] == "awaiting_approval", f"(status {status})")
        record("health OK with Redis down", http("GET", "/api/v1/health", timeout=10)[0] == 200)
    finally:
        compose("start", "redis")
        time.sleep(3)


def scenario_postgres_restart():
    compose("restart", "postgres")
    end = time.time() + 90
    while time.time() < end and http("GET", "/api/v1/plans")[0] != 200:
        time.sleep(2)
    failures = 0
    for _ in range(5):
        status, _ = plan()
        failures += status != 200
    record("plans succeed after a Postgres restart (pool recovered)", failures == 0, f"({failures}/5 failed)")


def psql(sql: str, db: str = "voyagent") -> str:
    return compose("exec", "-T", "postgres", "psql", "-U", "voyagent", "-d", db, "-tAc", sql).strip()


def scenario_backup_restore():
    status, paused = plan("4 day trip to Lisbon")
    assert status == 200, paused
    tid = paused["thread_id"]
    threads_before = int(psql("SELECT count(*) FROM app.threads"))

    dump = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "pg_dump", "-U", "voyagent", "-d", "voyagent", "-Fc"],
        capture_output=True, check=True,
    ).stdout
    record("pg_dump produced a backup", len(dump) > 10_000, f"({len(dump) // 1024} KiB)")

    psql("DROP DATABASE IF EXISTS voyagent_restore")
    psql("CREATE DATABASE voyagent_restore")
    restore = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "pg_restore", "-U", "voyagent", "-d", "voyagent_restore", "--no-owner"],
        input=dump, capture_output=True, check=False,
    )
    record("pg_restore into a fresh database", restore.returncode == 0, restore.stderr.decode()[-300:])
    threads_after = int(psql("SELECT count(*) FROM app.threads", db="voyagent_restore"))
    record("ownership rows intact", threads_after == threads_before, f"({threads_after}/{threads_before})")

    # Point a fresh orchestrator + api at the restored database and resume the paused plan there.
    script = (
        "import asyncio, os, graph, orchestrator\n"
        "from langgraph.types import Command\n"
        "async def go():\n"
        "    g, close = await graph.open_graph(os.environ['DATABASE_URL'])\n"
        f"    cfg = orchestrator.config('{tid}')\n"
        "    snap = await g.aget_state(cfg)\n"
        "    assert snap.next == ('human_approval',), snap.next\n"
        "    async for _ in g.astream(Command(resume={'approved': True, 'feedback': ''}), cfg, stream_mode='updates'):\n"
        "        pass\n"
        "    final = (await g.aget_state(cfg)).values['final_response']\n"
        "    await close()\n"
        "    print('RESUMED', len(final))\n"
        "asyncio.run(go())\n"
    )
    out = subprocess.run(
        ["docker", "compose", "run", "--rm", "--no-deps", "-T",
         "-e", "DATABASE_URL=postgresql://voyagent:voyagent_local_only@postgres:5432/voyagent_restore",
         "-e", "MOCK_MODE=true", "-e", "MOCK_DELAY=0", "-e", "CHECKPOINTER_SETUP=false",
         "orchestrator", "python", "-c", script],
        capture_output=True, text=True, check=False,
    )
    record("paused plan resumes to completion from the restored backup", "RESUMED" in out.stdout,
           (out.stdout + out.stderr)[-300:].replace("\n", " "))


def main():
    if not wait_healthy():
        sys.exit("api not healthy")
    for scenario in (scenario_orchestrator_crash, scenario_redis_down, scenario_postgres_restart, scenario_backup_restore):
        try:
            scenario()
        except Exception as e:  # noqa: BLE001 - report and continue with the next scenario
            record(scenario.__name__, False, f"error: {e}")
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
