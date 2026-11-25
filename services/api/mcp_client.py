"""MCP tool access.

Each service has one persistent MCP session per process (see SessionPool), opened on
first use with the server's key (keys.py). When a run has no key for a service, the call
returns dummy data from mock.py instead of contacting the real API.
"""

import asyncio
import contextvars
import logging
import os
import sys
import weakref
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

import agent_config
import guardrails
import keys
import metrics
import mock
import telemetry
import tool_format

log = logging.getLogger("voyagent.mcp")

PROJECT_DIR = Path(__file__).resolve().parent

# By default the stdio MCP servers run with the same Python as this app
# (aviationstack-mcp is installed from requirements.txt). Override with env vars
# if you run them from a separate environment.
AVIATIONSTACK_MCP_PYTHON = os.getenv("AVIATIONSTACK_MCP_PYTHON", sys.executable)
WEATHER_MCP_PYTHON = os.getenv("WEATHER_MCP_PYTHON", sys.executable)
TAVILY_MCP_URL = os.getenv("TAVILY_MCP_URL", "https://mcp.tavily.com/mcp/")
# stdio tool servers start under OS resource limits (sandbox_exec.py).
SANDBOX = str(PROJECT_DIR / "sandbox_exec.py")
WEATHER_MCP_SCRIPT = os.getenv("WEATHER_MCP_SCRIPT", str(PROJECT_DIR / "weather_mcp_server.py"))


# Only what a Python subprocess needs to start; nothing else from our environment
# (which holds every provider key and the database URL) reaches tool servers.
_PASSTHROUGH_ENV = (
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",  # Windows process basics
    "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",  # custom CA bundles
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
)


def _server_env(**extra: str) -> dict[str, str]:
    """Minimal environment for a stdio MCP server: process basics + that server's own key only."""
    env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra)
    return env


def _connection(service: str, key: str) -> dict:
    if service == "tavily":
        # Key in a header, not the URL: URLs end up in proxy/access logs (SECURITY.md F12).
        return {
            "transport": "streamable_http",
            "url": TAVILY_MCP_URL,
            "headers": {"Authorization": f"Bearer {key}"},
        }
    if service == "aviationstack":
        return {
            "transport": "stdio",
            "command": sys.executable,
            "args": [SANDBOX, AVIATIONSTACK_MCP_PYTHON, "-m", "aviationstack_mcp", "mcp", "run"],
            "env": _server_env(AVIATION_STACK_API_KEY=key),
        }
    if service == "openweather":
        return {
            "transport": "stdio",
            "command": sys.executable,
            "args": [SANDBOX, WEATHER_MCP_PYTHON, WEATHER_MCP_SCRIPT],
            "cwd": str(PROJECT_DIR),
            "env": _server_env(OPENWEATHER_API_KEY=key),
        }
    raise ValueError(f"Unknown MCP service: {service}")


CONNECT_TIMEOUT = float(os.getenv("MCP_CONNECT_TIMEOUT", "30"))
CALL_TIMEOUT = float(os.getenv("MCP_CALL_TIMEOUT", "30"))


class _Session:
    """One long-lived MCP session for a (service, key), reused by every request (SECURITY.md F7).

    Previously each tool call opened a new session, which for stdio servers meant a new
    subprocess per call. The session is entered and exited by its own background task
    (MCP/anyio transports must be closed by the task that opened them); requests use it
    concurrently. If it dies or a call times out it is discarded and the next call reconnects.
    """

    def __init__(self, service: str, key: str):
        self.service = service
        self.key = key
        self.tools: dict[str, object] | None = None
        self.error: BaseException | None = None
        self.closed = False
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        # Fresh context: the session outlives the request that happened to open it.
        self._task = asyncio.get_running_loop().create_task(
            self._run(), name=f"mcp-session:{service}", context=contextvars.Context()
        )

    async def _run(self) -> None:
        try:
            client = MultiServerMCPClient({self.service: _connection(self.service, self.key)})
            async with client.session(self.service) as session:
                self.tools = {t.name: t for t in await load_mcp_tools(session)}
                log.info("mcp_session_opened", extra={"service": self.service, "tools": len(self.tools)})
                self._ready.set()
                await self._stop.wait()
        except BaseException as e:  # noqa: BLE001 - recorded, surfaced to callers, task ends
            self.error = e
            if not isinstance(e, asyncio.CancelledError):
                log.error("mcp_session_failed", extra={"service": self.service, "error_type": type(e).__name__})
        finally:
            self.closed = True
            self._ready.set()

    async def get_tools(self) -> dict[str, object]:
        await asyncio.wait_for(self._ready.wait(), CONNECT_TIMEOUT)
        if self.tools is None or self.closed:
            # Never include the raw exception: it can contain the key.
            name = type(self.error).__name__ if self.error else "closed"
            raise RuntimeError(f"Could not connect to the {self.service} MCP server ({name})")
        return self.tools

    async def aclose(self) -> None:
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), 5)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        except Exception:  # noqa: BLE001,S110 - already logged in _run
            pass


class SessionPool:
    """Sessions for one event loop, at most one per service (a key change replaces it)."""

    def __init__(self):
        self._sessions: dict[str, _Session] = {}

    def _get(self, service: str, key: str) -> _Session:
        # No await between lookup and insert, so concurrent callers share one session.
        s = self._sessions.get(service)
        if s is None or s.closed or s.key != key:
            if s is not None:
                asyncio.get_running_loop().create_task(s.aclose())
            s = self._sessions[service] = _Session(service, key)
        return s

    def discard(self, session: _Session) -> None:
        if self._sessions.get(session.service) is session:
            del self._sessions[session.service]
        asyncio.get_running_loop().create_task(session.aclose())

    async def call(self, service: str, key: str, tool_name: str, args: dict):
        session = self._get(service, key)
        try:
            tools = await session.get_tools()
        except Exception:
            self.discard(session)
            raise
        tool = tools.get(tool_name)
        if tool is None:
            raise ValueError(f"Tool '{tool_name}' not found on {service} MCP server")
        try:
            return await asyncio.wait_for(tool.ainvoke(args), CALL_TIMEOUT)
        except TimeoutError:
            # A hung call may mean a dead server: reconnect next time.
            self.discard(session)
            raise RuntimeError(f"{service} MCP call timed out after {CALL_TIMEOUT:.0f}s") from None

    async def aclose(self) -> None:
        sessions, self._sessions = list(self._sessions.values()), {}
        await asyncio.gather(*(s.aclose() for s in sessions), return_exceptions=True)

    def __len__(self) -> int:
        return len(self._sessions)


# Sessions are bound to the event loop that opened them (one loop in production).
_pools: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, SessionPool]" = weakref.WeakKeyDictionary()


def pool() -> SessionPool:
    loop = asyncio.get_running_loop()
    if loop not in _pools:
        _pools[loop] = SessionPool()
    return _pools[loop]


async def aclose_sessions() -> None:
    """Close this loop's sessions (app shutdown)."""
    p = _pools.pop(asyncio.get_running_loop(), None)
    if p is not None:
        await p.aclose()


class ToolFailed(Exception):
    def __init__(self, status, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def call_tool(service: str, tool_name: str, args: dict | None = None) -> str:
    """Call a live MCP tool and return readable Markdown. Raises ToolFailed on provider errors."""
    key = getattr(keys.current(), service)
    try:
        raw = await pool().call(service, key, tool_name, args or {})
    except Exception as e:
        raise ToolFailed(None, keys.redact(str(e))) from None
    err = tool_format.provider_error(service, raw)
    if err:
        raise ToolFailed(err[0], keys.redact(err[1]))
    # Tool results are data: delimited and capped before any agent puts them in a prompt.
    return guardrails.tool_output(service, keys.redact(tool_format.normalize(service, tool_name, raw)))


# Key field -> service name used in the API's `modes`
_SERVICE = {"tavily": "hotels", "aviationstack": "flights", "openweather": "weather"}


async def _tool(key_field: str, tool_name: str, args: dict, dummy):
    """Live call when this run has a working key; otherwise (or on any failure) dummy data.

    A live failure (rejected key, rate limit, network) switches the rest of the run to
    dummy data, so the plan still completes through all agents.
    The calling agent must have the tool in its allow-list (agents/<name>.md), live or not.
    """
    try:
        agent_config.check_tool(key_field, tool_name)
    except agent_config.ToolNotAllowed:
        metrics.tool_calls.labels(key_field, "denied").inc()
        raise
    if keys.is_live(key_field):
        try:
            with telemetry.tracer.start_as_current_span("tool.call", attributes={"tool.service": key_field, "tool.name": tool_name}):
                result = await call_tool(key_field, tool_name, args)
            metrics.tool_calls.labels(key_field, "live").inc()
            return result
        except ToolFailed as e:
            metrics.tool_calls.labels(key_field, "fallback").inc()
            msg = tool_format.fallback_message(key_field, e.status, e.detail)
            log.warning("tool_fallback", extra={"service": key_field, "tool": tool_name, "status": e.status, "reason": msg})
            keys.trigger_fallback(_SERVICE[key_field], msg)
    keys.use_dummy()
    metrics.tool_calls.labels(key_field, "mock").inc()
    return await dummy()


# ------------------------
# Tools used by the agents
# ------------------------


async def tavily_search(query: str):
    return await _tool("tavily", "tavily_search", {"query": query}, lambda: mock.fake_tavily_search(query))


async def list_airports(search: str = "", limit: int = 10):
    return await _tool("aviationstack", "list_airports", {"search": search, "limit": limit, "offset": 0}, lambda: mock.fake_tool(search))


async def list_airlines(search: str = "", limit: int = 10):
    return await _tool("aviationstack", "list_airlines", {"search": search, "limit": limit, "offset": 0}, lambda: mock.fake_tool(search))


async def current_weather(city: str):
    return await _tool("openweather", "get_current_weather", {"city": city}, lambda: mock.fake_weather(city))


async def forecast(city: str):
    return await _tool("openweather", "get_forecast", {"city": city}, lambda: mock.fake_forecast(city))
