"""Tiny stdio MCP server for tests: reports its process ID, so a test can tell whether
calls share one subprocess. No network access."""

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pid")


@mcp.tool()
def pid() -> str:
    return str(os.getpid())


@mcp.tool()
def echo(text: str) -> str:
    return text


if __name__ == "__main__":
    mcp.run(transport="stdio")
