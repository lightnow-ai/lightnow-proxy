from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(value: str) -> str:
    return f"echo:{value}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
