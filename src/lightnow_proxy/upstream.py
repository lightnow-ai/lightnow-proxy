from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import os
import sys
from typing import AsyncIterator, TextIO

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, ReadResourceResult, Resource, ResourceTemplate, Tool

from lightnow_proxy.config import UpstreamConfig


class UpstreamMCPClient:
    def __init__(self, errlog: TextIO | None = None, *, suppress_stdio_stderr: bool = False):
        self.errlog = errlog if errlog is not None else sys.stderr
        self.suppress_stdio_stderr = suppress_stdio_stderr

    @asynccontextmanager
    async def session(self, config: UpstreamConfig) -> AsyncIterator[ClientSession]:
        if config.transport == "stdio":
            async with self._stdio_session(config) as session:
                yield session
            return

        if config.transport != "streamable-http" or config.url is None:
            raise ValueError(f"unsupported upstream transport: {config.transport}")

        http_client = httpx.AsyncClient(
            headers=config.resolved_headers(),
            timeout=httpx.Timeout(config.timeout_seconds),
        )
        async with http_client:
            async with streamable_http_client(str(config.url), http_client=http_client) as (
                read_stream,
                write_stream,
                _session_id,
            ):
                session = ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=config.timeout_seconds),
                )
                async with session:
                    await session.initialize()
                    yield session

    @asynccontextmanager
    async def _stdio_session(self, config: UpstreamConfig) -> AsyncIterator[ClientSession]:
        if not config.command:
            raise ValueError("stdio upstream requires command")

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
    ) -> AsyncIterator[ClientSession]:
        async with stdio_client(parameters, errlog=errlog) as (read_stream, write_stream):
            session = ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=config.timeout_seconds),
            )
            async with session:
                await session.initialize()
                yield session

    async def list_tools(self, config: UpstreamConfig) -> list[Tool]:
        async with self.session(config) as session:
            result = await session.list_tools()
            return list(result.tools)

    async def call_tool(self, config: UpstreamConfig, tool_name: str, arguments: dict | None) -> CallToolResult:
        async with self.session(config) as session:
            result = await session.call_tool(tool_name, arguments=arguments or {})
            if isinstance(result, CallToolResult):
                return result
            return CallToolResult.model_validate(result)

    async def list_resources(self, config: UpstreamConfig) -> list[Resource]:
        async with self.session(config) as session:
            result = await session.list_resources()
            return list(result.resources)

    async def list_resource_templates(self, config: UpstreamConfig) -> list[ResourceTemplate]:
        async with self.session(config) as session:
            result = await session.list_resource_templates()
            return list(result.resourceTemplates)

    async def read_resource(self, config: UpstreamConfig, uri: str) -> ReadResourceResult:
        async with self.session(config) as session:
            result = await session.read_resource(uri)
            if isinstance(result, ReadResourceResult):
                return result
            return ReadResourceResult.model_validate(result)
