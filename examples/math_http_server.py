from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import Receive, Scope, Send
import uvicorn


class MCPApp:
    def __init__(self, manager: StreamableHTTPSessionManager):
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.manager.handle_request(scope, receive, send)


def build_server() -> Server:
    server = Server("math-upstream", version="0.1.0")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="add",
                description="Add two integers",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "left": {"type": "integer"},
                        "right": {"type": "integer"},
                    },
                    "required": ["left", "right"],
                },
            )
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        if name != "add":
            return CallToolResult(content=[TextContent(type="text", text=f"unknown tool: {name}")], isError=True)
        payload = arguments or {}
        total = int(payload["left"]) + int(payload["right"])
        return CallToolResult(content=[TextContent(type="text", text=str(total))])

    return server


def create_app() -> Starlette:
    manager = StreamableHTTPSessionManager(
        app=build_server(),
        json_response=False,
        stateless=True,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Route("/mcp", MCPApp(manager))], lifespan=lifespan)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
