from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from dataclasses import dataclass
import json
import os
import sys
from time import monotonic
from typing import AsyncIterator, Awaitable, Callable, TextIO

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, ReadResourceResult, Resource, ResourceTemplate, Tool
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from lightnow_proxy.config import UpstreamConfig
from lightnow_proxy.diagnostics import validate_upstream_config
from lightnow_proxy.runtime_files import prepare_runtime_files

DISCOVERY_TIMEOUT_SECONDS = 30
DISCOVERY_MAX_PAGES = 100
DISCOVERY_MAX_TOOLS = 2000
DISCOVERY_MAX_BYTES = 4 * 1024 * 1024
DISCOVERY_MAX_CURSOR_BYTES = 4096


class ToolDiscoveryError(Exception):
    """Content-free failure; a partial manifest must never become complete."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, repr=False)
class ToolInventory:
    tools: list[Tool]
    page_count: int
    duration_ms: int


class UpstreamMCPClient:
    def __init__(self, errlog: TextIO | None = None, *, suppress_stdio_stderr: bool = False):
        self.errlog = errlog if errlog is not None else sys.stderr
        self.suppress_stdio_stderr = suppress_stdio_stderr

    @asynccontextmanager
    async def session(self, config: UpstreamConfig) -> AsyncIterator[Client]:
        if config.transport == "stdio":
            async with self._stdio_session(config) as session:
                yield session
            return

        if config.transport != "streamable-http" or config.url is None:
            raise ValueError(f"unsupported upstream transport: {config.transport}")

        http_client = httpx2.AsyncClient(
            headers=config.resolved_headers(),
            timeout=httpx2.Timeout(config.timeout_seconds),
        )
        async with http_client:
            transport = streamable_http_client(str(config.url), http_client=http_client)
            async with Client(transport, read_timeout_seconds=config.timeout_seconds) as client:
                yield client

    @asynccontextmanager
    async def _stdio_session(self, config: UpstreamConfig) -> AsyncIterator[Client]:
        if not config.command:
            raise ValueError("stdio upstream requires command")

        config = prepare_runtime_files(config)
        validate_upstream_config(config)

        parameters = StdioServerParameters(
            command=config.command,
            args=config.args,
            env=config.resolved_env() or None,
            cwd=config.cwd,
        )
        if self.suppress_stdio_stderr:
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with self._stdio_client_session(parameters, config, errlog) as session:
                    yield session
            return

        async with self._stdio_client_session(parameters, config, self.errlog) as session:
            yield session

    @asynccontextmanager
    async def _stdio_client_session(
        self,
        parameters: StdioServerParameters,
        config: UpstreamConfig,
        errlog: TextIO,
    ) -> AsyncIterator[Client]:
        transport = stdio_client(parameters, errlog=errlog)
        async with Client(transport, read_timeout_seconds=config.timeout_seconds) as client:
            yield client

    async def list_tools(self, config: UpstreamConfig) -> list[Tool]:
        async with self.session(config) as client:
            return (await self.discover_tools(client)).tools

    async def discover_tools(self, client: Client) -> ToolInventory:
        """Complete fresh pagination on the caller's actual configured client.

        JSON serialization here only bounds metadata size. Registry owns the
        canonical fingerprints; this client does not invent a second hash codec.
        """
        started = monotonic()
        tools: list[Tool] = []
        names: set[str] = set()
        cursors: set[str] = set()
        cursor = None
        size = 2  # JSON array delimiters
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
                for page in range(1, DISCOVERY_MAX_PAGES + 1):
                    result = await client.list_tools(cursor=cursor, cache_mode="bypass")
                    for tool in result.tools:
                        if not tool.name or len(tool.name) > 255 or tool.name in names:
                            raise ToolDiscoveryError("INVALID_TOOL_MANIFEST")
                        names.add(tool.name)
                        try:
                            serialized = json.dumps(
                                tool.model_dump(by_alias=True, mode="json", exclude_none=True),
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        except (TypeError, ValueError):
                            raise ToolDiscoveryError("INVALID_TOOL_MANIFEST") from None
                        size += len(serialized) + (1 if tools else 0)
                        if size > DISCOVERY_MAX_BYTES or len(tools) >= DISCOVERY_MAX_TOOLS:
                            raise ToolDiscoveryError("DISCOVERY_LIMIT_EXCEEDED")
                        tools.append(tool)
                    cursor = result.next_cursor
                    if cursor is None:
                        duration_ms = int((monotonic() - started) * 1000)
                        if duration_ms > DISCOVERY_TIMEOUT_SECONDS * 1000:
                            raise ToolDiscoveryError("DISCOVERY_TIMED_OUT")
                        return ToolInventory(tools, page, duration_ms)
                    if len(cursor.encode("utf-8")) > DISCOVERY_MAX_CURSOR_BYTES:
                        raise ToolDiscoveryError("DISCOVERY_LIMIT_EXCEEDED")
                    if cursor in cursors:
                        raise ToolDiscoveryError("INVALID_TOOL_MANIFEST")
                    cursors.add(cursor)
            raise ToolDiscoveryError("DISCOVERY_LIMIT_EXCEEDED")
        except TimeoutError:
            raise ToolDiscoveryError("DISCOVERY_TIMED_OUT") from None

    async def call_tool(
        self,
        config: UpstreamConfig,
        tool_name: str,
        arguments: dict | None,
        *,
        authorize: Callable[[ToolInventory], Awaitable[None]] | None = None,
    ) -> CallToolResult:
        async with self.session(config) as client:
            if authorize is not None:
                await authorize(await self.discover_tools(client))
                # The documented session API sends once. The high-level client
                # can retry HEADER_MISMATCH or input-required rounds against a
                # new definition without returning to our current policy gate.
                result = await client.session.call_tool(tool_name, arguments=arguments or {})
            else:
                if config.transport == "streamable-http" and client.protocol_version in MODERN_PROTOCOL_VERSIONS:
                    # Modern headers derive from actual tools/list schemas.
                    await self.discover_tools(client)
                result = await client.call_tool(tool_name, arguments=arguments or {})
            if isinstance(result, CallToolResult):
                return result
            return CallToolResult.model_validate(result)

    async def list_resources(self, config: UpstreamConfig) -> list[Resource]:
        async with self.session(config) as client:
            result = await client.list_resources()
            return list(result.resources)

    async def list_resource_templates(self, config: UpstreamConfig) -> list[ResourceTemplate]:
        async with self.session(config) as client:
            result = await client.list_resource_templates()
            return list(result.resource_templates)

    async def read_resource(self, config: UpstreamConfig, uri: str) -> ReadResourceResult:
        async with self.session(config) as client:
            result = await client.read_resource(uri)
            if isinstance(result, ReadResourceResult):
                return result
            return ReadResourceResult.model_validate(result)
