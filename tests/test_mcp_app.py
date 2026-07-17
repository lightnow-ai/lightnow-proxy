from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import socket
import sys
from typing import Any, AsyncIterator
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.client.streamable_http import streamable_http_client
from mcp import types
from mcp.types import CallToolResult, TextContent, Tool
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import Receive, Scope, Send
import uvicorn

from lightnow_proxy import __version__
from lightnow_proxy.app import create_app, register_unvalidated_call_tool_handler
from lightnow_proxy.auth import Principal
from lightnow_proxy.config import (
    AuthConfig,
    ProfileConfig,
    ProxyConfig,
    RegistryApiConfig,
    RuntimeUpstreamConfig,
    ServerConfig,
    UpstreamConfig,
)
from lightnow_proxy.health import build_health_report
from lightnow_proxy.registry import ResolvedRuntimeUpstream
from lightnow_proxy.upstream import UpstreamMCPClient


class FakeUpstreamClient:
    async def list_tools(self, config: UpstreamConfig) -> list[Tool]:
        host = str(config.url.host) if config.url is not None else str(config.command)
        if "grafana" in host:
            return [Tool(name="query_loki_logs", description="Query logs", inputSchema={"type": "object"})]
        return [Tool(name="search_files", description="Search files", inputSchema={"type": "object"})]

    async def call_tool(self, config: UpstreamConfig, tool_name: str, arguments: dict | None) -> CallToolResult:
        assert config.url is not None
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"{config.url.host}:{tool_name}:{arguments or {}}",
                )
            ]
        )


class FailingUpstreamClient(FakeUpstreamClient):
    async def list_tools(self, config: UpstreamConfig) -> list[Tool]:
        host = str(config.url.host) if config.url is not None else str(config.command)
        if "grafana" in host:
            raise TimeoutError("upstream did not answer")
        return await super().list_tools(config)


class FakeRegistryClient:
    def __init__(self) -> None:
        self.principals: list[Principal | None] = []

    async def resolve_upstream(
        self,
        upstream: RuntimeUpstreamConfig,
        principal: Principal | None,
    ) -> ResolvedRuntimeUpstream:
        self.principals.append(principal)
        return ResolvedRuntimeUpstream(
            name=upstream.name,
            config=UpstreamConfig(url=f"https://{upstream.name}.example.test/mcp"),
        )


class FakeRuntimeEventRegistry:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def post_runtime_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


@pytest.mark.asyncio
async def test_status_endpoint_reports_static_runtime_state() -> None:
    config = build_config()
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as client:
        response = await client.get("/status")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_status_endpoint_can_run_active_upstream_check() -> None:
    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={"enabled": True, "profile": "default"},
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        profiles={"default": ProfileConfig()},
        upstreams={},
    )
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as client:
        response = await client.get("/status?check=upstreams")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["profiles"][0]["name"] == "default"
    assert payload["profiles"][0]["warning"] == "profile has no upstream MCP servers"


@pytest.mark.asyncio
async def test_status_endpoint_emits_proxy_health_event_for_active_checks() -> None:
    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={
            "enabled": True,
            "profile": "default",
            "client_name": "codex",
            "client_transport": "stdio",
            "policy_mode": "enforce",
            "allow_unmanaged_client_servers": False,
            "device_installation_id": "11111111-1111-4111-8111-111111111111",
            "client_instance_id": "22222222-2222-4222-8222-222222222222",
        },
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig()},
        upstreams={},
    )
    from lightnow_proxy.router import ToolRouter

    router = ToolRouter(config)
    registry = FakeRuntimeEventRegistry()
    router.registry_client = registry
    app = create_app(config, router=router)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as client:
        response = await client.get("/status?check=upstreams")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert len(registry.events) == 1
    event = registry.events[0]
    assert event["event_type"] == "proxy_health"
    assert event["status"] == "error"
    assert event["profile"] == "default"
    assert event["mcp_client_name"] == "codex"
    assert event["runner_name"] == "lightnow-local-proxy"
    assert event["runner_version"] == __version__
    assert event["proxy_health_status"] == "degraded"
    assert event["proxy_upstreams"] == 0
    assert event["proxy_healthy_upstreams"] == 0
    assert event["proxy_failed_upstreams"] == 0
    assert event["proxy_tool_count"] == 0
    assert event["local_proxy_policy_mode"] == "enforce"
    assert event["local_proxy_allow_unmanaged_client_servers"] is False


@pytest.mark.asyncio
async def test_status_reports_non_secret_local_proxy_runtime_state() -> None:
    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.profile = "admins"
    config.local_proxy.client_name = "codex"
    config.local_proxy.client_transport = "stdio"
    config.local_proxy.policy_mode = "enforce"
    config.local_proxy.allow_unmanaged_client_servers = False
    config.registry_api = RegistryApiConfig(
        enabled=True,
        base_url="https://registry-api.example.test",
        use_cli_session=True,
    )
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as client:
        response = await client.get("/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["mode"] == "local_proxy"
    assert payload["runner"] == {
        "name": "lightnow-local-proxy",
        "version": __version__,
    }
    assert payload["local_proxy"]["profile"] == "admins"
    assert payload["local_proxy"]["client_name"] == "codex"
    assert payload["local_proxy"]["client_transport"] == "stdio"
    assert payload["local_proxy"]["policy_mode"] == "enforce"
    assert payload["local_proxy"]["allow_unmanaged_client_servers"] is False
    assert payload["local_proxy"]["telemetry_enabled"] is True
    assert payload["registry_api"]["enabled"] is True
    assert payload["registry_api"]["use_cli_session"] is True
    assert payload["profiles"] == ["admins", "users"]
    assert payload["static_upstreams"] == ["grafana", "nextcloud"]
    assert "token" not in str(payload).lower()
    assert "secret" not in str(payload).lower()


@pytest.mark.asyncio
async def test_device_heartbeat_loop_sends_immediately_then_waits_two_minutes() -> None:
    """Presence starts immediately and the regular interval is the only retry loop."""

    class StopHeartbeatLoop(Exception):
        pass

    class FakeRuntimeRegistry:
        def __init__(self) -> None:
            self.heartbeats: list[tuple[str, str, dict[str, Any]]] = []

        async def post_device_heartbeat(
            self,
            installation_id: str,
            client_instance_id: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            self.heartbeats.append((installation_id, client_instance_id, payload))
            return {"heartbeat_interval_seconds": 120}

    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.profile = "research"
    config.local_proxy.client_name = "codex"
    config.local_proxy.client_transport = "stdio"
    config.local_proxy.device_installation_id = "11111111-1111-4111-8111-111111111111"
    config.local_proxy.client_instance_id = "22222222-2222-4222-8222-222222222222"
    config.local_proxy.device_hostname = "mac-01"
    config.local_proxy.device_platform = "macos"

    from lightnow_proxy.router import ToolRouter

    router = ToolRouter(config)
    registry = FakeRuntimeRegistry()
    router.registry_client = registry
    with patch("lightnow_proxy.router.asyncio.sleep", side_effect=StopHeartbeatLoop) as sleep:
        with pytest.raises(StopHeartbeatLoop):
            await router.run_device_heartbeat_loop()

    assert sleep.call_args.args == (120,)
    assert len(registry.heartbeats) == 1
    installation_id, client_instance_id, payload = registry.heartbeats[0]
    assert installation_id == "11111111-1111-4111-8111-111111111111"
    assert client_instance_id == "22222222-2222-4222-8222-222222222222"
    assert payload["device"] == {
        "reported_hostname": "mac-01",
        "platform": "macos",
    }
    assert payload["client"]["profile"] == "research"
    assert payload["client"]["runner_version"] == __version__
    assert router.device_heartbeat_status()["last_success_at"] is not None


@pytest.mark.asyncio
async def test_device_heartbeat_failure_is_diagnostic_and_does_not_raise() -> None:
    """Heartbeat failures remain local diagnostics and never block MCP operations."""

    class FailingRegistry:
        async def post_device_heartbeat(self, *_args, **_kwargs):
            raise httpx.ConnectError("registry unavailable")

    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.device_installation_id = "11111111-1111-4111-8111-111111111111"
    config.local_proxy.client_instance_id = "22222222-2222-4222-8222-222222222222"
    config.local_proxy.device_hostname = "pc-4711"

    from lightnow_proxy.router import ToolRouter

    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    router.registry_client = FailingRegistry()

    assert await router.send_device_heartbeat() is False
    assert router.device_heartbeat_status()["last_error"] == "registry unavailable"
    assert await router.list_tools("admins")


@pytest.mark.asyncio
async def test_device_heartbeat_is_disabled_with_runtime_telemetry() -> None:
    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.telemetry_enabled = False
    config.local_proxy.device_installation_id = "11111111-1111-4111-8111-111111111111"
    config.local_proxy.client_instance_id = "22222222-2222-4222-8222-222222222222"
    config.local_proxy.device_hostname = "mac-02"

    from lightnow_proxy.router import ToolRouter

    router = ToolRouter(config)
    assert router.device_heartbeat_enabled() is False
    assert await router.send_device_heartbeat() is False


@pytest.mark.asyncio
async def test_health_reports_profile_upstream_status_without_secrets() -> None:
    from lightnow_proxy.router import ToolRouter

    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.profile = "admins"
    config.local_proxy.client_name = "codex"
    config.local_proxy.client_transport = "stdio"
    router = ToolRouter(config, upstream_client=FakeUpstreamClient())

    payload = await build_health_report(config, router)
    assert payload["status"] == "healthy"
    assert payload["mode"] == "local_proxy"
    assert payload["summary"] == {
        "profiles": 1,
        "upstreams": 2,
        "healthy_upstreams": 2,
        "failed_upstreams": 0,
        "tools": 2,
    }
    assert payload["local_proxy"]["client_name"] == "codex"
    assert {upstream["name"] for upstream in payload["profiles"][0]["upstreams"]} == {"grafana", "nextcloud"}
    assert all(upstream["status"] == "healthy" for upstream in payload["profiles"][0]["upstreams"])
    assert "token" not in str(payload).lower()
    assert "secret" not in str(payload).lower()


@pytest.mark.asyncio
async def test_proxy_health_event_summarizes_active_upstream_report() -> None:
    from lightnow_proxy.router import ToolRouter

    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.profile = "admins"
    config.local_proxy.client_name = "codex"
    config.local_proxy.client_transport = "stdio"
    config.local_proxy.policy_mode = "enforce"
    config.local_proxy.allow_unmanaged_client_servers = False
    config.registry_api = RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test")
    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    registry = FakeRuntimeEventRegistry()
    router.registry_client = registry

    report = await build_health_report(config, router)
    await router.emit_health_event(report)

    assert len(registry.events) == 1
    event = registry.events[0]
    assert event["event_type"] == "proxy_health"
    assert event["status"] == "success"
    assert event["profile"] == "admins"
    assert event["mcp_client_name"] == "codex"
    assert event["client_transport"] == "stdio"
    assert event["runner_version"] == __version__
    assert event["proxy_health_status"] == "healthy"
    assert event["proxy_upstreams"] == 2
    assert event["proxy_healthy_upstreams"] == 2
    assert event["proxy_failed_upstreams"] == 0
    assert event["proxy_tool_count"] == 2
    assert event["local_proxy_policy_mode"] == "enforce"
    assert event["local_proxy_allow_unmanaged_client_servers"] is False


@pytest.mark.asyncio
async def test_health_reports_degraded_when_an_upstream_fails() -> None:
    from lightnow_proxy.router import ToolRouter

    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.profile = "admins"
    router = ToolRouter(config, upstream_client=FailingUpstreamClient())

    payload = await build_health_report(config, router)
    assert payload["status"] == "degraded"
    assert payload["summary"]["healthy_upstreams"] == 1
    assert payload["summary"]["failed_upstreams"] == 1
    upstreams = {upstream["name"]: upstream for upstream in payload["profiles"][0]["upstreams"]}
    assert upstreams["grafana"]["status"] == "error"
    assert upstreams["grafana"]["error_type"] == "TimeoutError"
    assert upstreams["grafana"]["diagnostic_code"] == "UPSTREAM_TIMEOUT"
    assert upstreams["grafana"]["diagnostic_kind"] == "timeout"
    assert upstreams["grafana"]["diagnostic_summary"] == "The MCP server did not respond before the timeout."
    assert "Check that the server is reachable" in upstreams["grafana"]["remediation"]
    assert upstreams["nextcloud"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_missing_docker_secret_becomes_actionable_health_telemetry(monkeypatch) -> None:
    from lightnow_proxy.router import ToolRouter

    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={"enabled": True, "profile": "default", "client_name": "codex"},
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig(upstreams=["github"])},
        upstreams={
            "github": UpstreamConfig(
                transport="stdio",
                command="docker",
                args=[
                    "run",
                    "--rm",
                    "-i",
                    "-e",
                    "GITHUB_PERSONAL_ACCESS_TOKEN",
                    "ghcr.io/github/github-mcp-server",
                ],
            ),
        },
    )
    router = ToolRouter(config)
    registry = FakeRuntimeEventRegistry()
    router.registry_client = registry

    report = await build_health_report(config, router)
    await router.emit_health_event(report)

    failure = report["profiles"][0]["upstreams"][0]
    assert failure["error_type"] == "UpstreamConfigurationError"
    assert failure["diagnostic_code"] == "DOCKER_ENV_MISSING"
    assert failure["diagnostic_kind"] == "configuration"
    assert failure["diagnostic_summary"] == "A Docker environment variable required by this MCP server is not available."
    assert failure["remediation"] == (
        "Configure GITHUB_PERSONAL_ACCESS_TOKEN as a managed or external secret in the Runtime Profile."
    )
    assert registry.events[0]["proxy_health_failures"][0]["diagnostic_code"] == "DOCKER_ENV_MISSING"
    assert "managed or external secret" in registry.events[0]["proxy_health_failures"][0]["remediation"]


@pytest.mark.asyncio
async def test_proxy_health_event_reports_redacted_upstream_failures() -> None:
    from lightnow_proxy.router import ToolRouter

    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.profile = "admins"
    config.local_proxy.client_name = "codex"
    config.registry_api = RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test")
    router = ToolRouter(config, upstream_client=FailingUpstreamClient())
    registry = FakeRuntimeEventRegistry()
    router.registry_client = registry

    report = await build_health_report(config, router)
    await router.emit_health_event(report)

    event = registry.events[0]
    assert event["proxy_health_status"] == "degraded"
    assert event["proxy_failed_upstreams"] == 1
    assert event["proxy_health_failures"] == [
        {
            "profile": "admins",
            "name": "grafana",
            "transport": "streamable-http",
            "status": "error",
            "duration_ms": event["proxy_health_failures"][0]["duration_ms"],
            "error_type": "TimeoutError",
            "error_message": "upstream did not answer",
            "diagnostic_code": "UPSTREAM_TIMEOUT",
            "diagnostic_kind": "timeout",
            "diagnostic_summary": "The MCP server did not respond before the timeout.",
            "remediation": (
                "Check that the server is reachable and increase the configured timeout only if startup is "
                "expected to be slow."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_health_unwraps_exception_groups_to_actual_cause() -> None:
    from lightnow_proxy.router import ToolRouter

    class GroupFailingUpstreamClient(FakeUpstreamClient):
        async def list_tools(self, config: UpstreamConfig) -> list[Tool]:
            host = str(config.url.host) if config.url is not None else str(config.command)
            if "grafana" in host:
                raise ExceptionGroup(
                    "unhandled errors in a TaskGroup",
                    [ExceptionGroup("nested", [ConnectionError("connection refused")])],
                )
            return await super().list_tools(config)

    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.profile = "admins"
    router = ToolRouter(config, upstream_client=GroupFailingUpstreamClient())

    payload = await build_health_report(config, router)
    upstreams = {upstream["name"]: upstream for upstream in payload["profiles"][0]["upstreams"]}
    assert upstreams["grafana"]["status"] == "error"
    assert upstreams["grafana"]["error_type"] == "ConnectionError"
    assert upstreams["grafana"]["error_message"] == "connection refused"


@pytest.mark.asyncio
async def test_local_proxy_lifespan_emits_runner_lifecycle_events() -> None:
    class FakeRuntimeRegistry:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        async def post_runtime_event(self, payload: dict[str, Any]) -> None:
            self.events.append(payload)

    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={
            "enabled": True,
            "profile": "default",
            "sync_from_lightnow": True,
            "client_name": "codex",
            "client_transport": "stdio",
            "policy_mode": "enforce",
            "allow_unmanaged_client_servers": False,
            "device_installation_id": "11111111-1111-4111-8111-111111111111",
            "client_instance_id": "22222222-2222-4222-8222-222222222222",
        },
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig()},
        upstreams={},
    )

    from lightnow_proxy.router import ToolRouter

    router = ToolRouter(config)
    registry = FakeRuntimeRegistry()
    router.registry_client = registry
    app = create_app(config, router=router)

    async with app.router.lifespan_context(app):
        pass

    assert [event["event_type"] for event in registry.events] == [
        "runner_started",
        "runner_stopped",
    ]
    assert registry.events[0]["profile"] == "default"
    assert registry.events[0]["status"] == "success"
    assert registry.events[0]["mcp_client_name"] == "codex"
    assert registry.events[0]["runner_name"] == "lightnow-local-proxy"
    assert registry.events[0]["runner_version"] == __version__
    assert registry.events[0]["local_proxy_policy_mode"] == "enforce"
    assert registry.events[0]["local_proxy_allow_unmanaged_client_servers"] is False
    assert registry.events[0]["device_installation_id"] == "11111111-1111-4111-8111-111111111111"
    assert registry.events[0]["client_instance_id"] == "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
async def test_local_proxy_telemetry_can_be_disabled() -> None:
    class FakeRuntimeRegistry:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        async def post_runtime_event(self, payload: dict[str, Any]) -> None:
            self.events.append(payload)

    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={
            "enabled": True,
            "profile": "default",
            "sync_from_lightnow": True,
            "client_name": "codex",
            "client_transport": "stdio",
            "telemetry_enabled": False,
        },
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig()},
        upstreams={},
    )

    from lightnow_proxy.router import ToolRouter

    router = ToolRouter(config)
    registry = FakeRuntimeRegistry()
    router.registry_client = registry
    app = create_app(config, router=router)

    async with app.router.lifespan_context(app):
        pass

    assert registry.events == []


@pytest.mark.asyncio
async def test_mcp_profile_lists_and_calls_prefixed_tools() -> None:
    config = build_config()
    from lightnow_proxy.router import ToolRouter

    app = create_app(config, router=ToolRouter(config, upstream_client=FakeUpstreamClient()))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as http_client:
            http_client.headers["Authorization"] = "Bearer admin-token"
            async with streamable_http_client(
                "http://localhost:8080/profiles/admins/mcp",
                http_client=http_client,
            ) as (read_stream, write_stream, _session_id):
                session = ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=5))
                async with session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert names == {"grafana__query_loki_logs", "nextcloud__search_files"}

                    result = await session.call_tool("grafana__query_loki_logs", arguments={"logql": '{job="x"}'})
                    assert result.isError is False
                    assert "grafana.example.test:query_loki_logs" in result.content[0].text


@pytest.mark.asyncio
async def test_mcp_profile_enforces_groups() -> None:
    config = build_config()
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as http_client:
            http_client.headers["Authorization"] = "Bearer user-token"
            response = await http_client.post(
                "/profiles/admins/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.1.0"},
                    },
                },
            )
            assert response.status_code == 403


@pytest.mark.asyncio
async def test_mcp_profile_resolves_runtime_upstreams_with_principal() -> None:
    config = build_runtime_config()
    from lightnow_proxy.router import ToolRouter

    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    registry_client = FakeRegistryClient()
    router.registry_client = registry_client
    app = create_app(config, router=router)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as http_client:
            http_client.headers["Authorization"] = "Bearer admin-token"
            async with streamable_http_client(
                "http://localhost:8080/profiles/admins/mcp",
                http_client=http_client,
            ) as (read_stream, write_stream, _session_id):
                session = ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=5))
                async with session:
                    await session.initialize()
                    tools = await session.list_tools()

    assert {tool.name for tool in tools.tools} == {"grafana__query_loki_logs"}
    assert registry_client.principals[0] is not None
    assert registry_client.principals[0].username == "nils.friedrichs"


@pytest.mark.asyncio
async def test_local_proxy_endpoint_lists_and_calls_selected_profile_tools() -> None:
    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.profile = "admins"

    from lightnow_proxy.router import ToolRouter

    app = create_app(config, router=ToolRouter(config, upstream_client=FakeUpstreamClient()))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as http_client:
            async with streamable_http_client(
                "http://127.0.0.1:8080/mcp",
                http_client=http_client,
            ) as (read_stream, write_stream, _session_id):
                session = ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=5))
                async with session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert names == {"grafana__query_loki_logs", "nextcloud__search_files"}

                    result = await session.call_tool("nextcloud__search_files", arguments={"query": "m2"})
                    assert result.isError is False
                    assert "nextcloud.example.test:search_files" in result.content[0].text


@pytest.mark.asyncio
async def test_local_proxy_endpoint_rejects_non_local_host_and_origin() -> None:
    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.profile = "admins"

    from lightnow_proxy.router import ToolRouter

    app = create_app(config, router=ToolRouter(config, upstream_client=FakeUpstreamClient()))
    transport = httpx.ASGITransport(app=app)
    initialize_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1.0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://evil.example.test") as http_client:
            response = await http_client.post("/mcp", json=initialize_payload, headers=headers)
            assert response.status_code == 421

        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as http_client:
            response = await http_client.post(
                "/mcp",
                json=initialize_payload,
                headers={**headers, "Origin": "http://evil.example.test"},
            )
            assert response.status_code == 403

            response = await http_client.post(
                "/mcp",
                json=initialize_payload,
                headers={**headers, "Origin": "http://127.0.0.1:8080"},
            )
            assert response.status_code == 200


@pytest.mark.asyncio
async def test_local_proxy_endpoint_ignores_non_loopback_public_url_for_browser_origins() -> None:
    config = build_config()
    config.server.public_url = "https://proxy.example.test"
    config.local_proxy.enabled = True
    config.local_proxy.profile = "admins"

    from lightnow_proxy.router import ToolRouter

    app = create_app(config, router=ToolRouter(config, upstream_client=FakeUpstreamClient()))
    transport = httpx.ASGITransport(app=app)
    initialize_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1.0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="https://proxy.example.test") as http_client:
            response = await http_client.post(
                "/mcp",
                json=initialize_payload,
                headers={**headers, "Origin": "https://proxy.example.test"},
            )
            assert response.status_code == 421


@pytest.mark.asyncio
async def test_profile_proxy_endpoint_allows_configured_public_url_with_bearer_auth() -> None:
    config = build_config()
    config.server.public_url = "https://proxy.example.test"

    from lightnow_proxy.router import ToolRouter

    app = create_app(config, router=ToolRouter(config, upstream_client=FakeUpstreamClient()))
    transport = httpx.ASGITransport(app=app)
    initialize_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1.0"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer admin-token",
        "Origin": "https://proxy.example.test",
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="https://proxy.example.test") as http_client:
            response = await http_client.post("/profiles/admins/mcp", json=initialize_payload, headers=headers)
            assert response.status_code == 200

            response = await http_client.post(
                "/profiles/admins/mcp",
                json=initialize_payload,
                headers={**headers, "Origin": "https://evil.example.test"},
            )
            assert response.status_code == 403


@pytest.mark.asyncio
async def test_local_proxy_call_tool_does_not_force_tool_listing() -> None:
    config = build_config()
    config.local_proxy.enabled = True
    config.local_proxy.profile = "admins"
    calls: list[str] = []

    class CallOnlyRouter:
        async def list_tools(self, profile_name: str) -> list[Tool]:
            calls.append("list_tools")
            return [
                Tool(
                    name="jenkins__getStatus",
                    description="Get Jenkins status",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]

        async def call_tool(
            self,
            profile_name: str,
            name: str,
            arguments: dict[str, Any] | None = None,
            request_meta: dict[str, Any] | None = None,
        ) -> CallToolResult:
            calls.append("call_tool")
            assert profile_name == "admins"
            assert name == "jenkins__getStatus"
            assert arguments == {}
            assert request_meta is None
            return CallToolResult(content=[TextContent(type="text", text="ok")])

    app = create_app(config, router=CallOnlyRouter())
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as http_client:
            async with streamable_http_client(
                "http://127.0.0.1:8080/mcp",
                http_client=http_client,
            ) as (read_stream, write_stream, _session_id):
                session = ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=5))
                async with session:
                    await session.initialize()
                    result = await session.call_tool("jenkins__getStatus", arguments={})

    assert result.isError is False
    assert result.content[0].text == "ok"
    assert calls == ["call_tool", "list_tools"]


@pytest.mark.asyncio
async def test_unvalidated_call_tool_handler_forwards_request_meta() -> None:
    server: Server = Server("test")
    captured: dict[str, Any] = {}

    async def handler(name: str, arguments: dict[str, Any], request_meta: dict[str, Any] | None) -> CallToolResult:
        captured["name"] = name
        captured["arguments"] = arguments
        captured["request_meta"] = request_meta
        return CallToolResult(content=[TextContent(type="text", text="ok")])

    register_unvalidated_call_tool_handler(server, handler)
    request = types.CallToolRequest.model_validate(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "jenkins__getStatus",
                "arguments": {"jobFullName": "lightnow-ai/registry-api/master"},
                "_meta": {
                    "threadId": "thread-1",
                    "x-codex-turn-metadata": {
                        "model": "gpt-5.5",
                        "turn_id": "turn-1",
                    },
                },
            },
        }
    )

    result = await server.request_handlers[types.CallToolRequest](request)

    assert result.root.content[0].text == "ok"
    assert captured == {
        "name": "jenkins__getStatus",
        "arguments": {"jobFullName": "lightnow-ai/registry-api/master"},
        "request_meta": {
            "threadId": "thread-1",
            "x-codex-turn-metadata": {
                "model": "gpt-5.5",
                "turn_id": "turn-1",
            },
        },
    }


@pytest.mark.asyncio
async def test_stdio_upstream_lists_and_calls_tools(tmp_path) -> None:
    server = tmp_path / "echo_server.py"
    server.write_text(
        """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")

@mcp.tool()
def echo(value: str) -> str:
    return f"echo:{value}"

if __name__ == "__main__":
    mcp.run(transport="stdio")
""".lstrip(),
        encoding="utf-8",
    )
    upstream = UpstreamConfig(
        transport="stdio",
        command=sys.executable,
        args=[str(server)],
        timeout_seconds=5,
    )

    client = UpstreamMCPClient()
    tools = await client.list_tools(upstream)
    assert [tool.name for tool in tools] == ["echo"]

    result = await client.call_tool(upstream, "echo", {"value": "ok"})
    assert result.isError is False
    assert result.content[0].text == "echo:ok"


@pytest.mark.asyncio
async def test_local_proxy_routes_to_stdio_and_streamable_http_upstreams(tmp_path) -> None:
    stdio_server = tmp_path / "echo_server.py"
    stdio_server.write_text(
        """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")

@mcp.tool()
def echo(value: str) -> str:
    return f"echo:{value}"

if __name__ == "__main__":
    mcp.run(transport="stdio")
""".lstrip(),
        encoding="utf-8",
    )

    async with run_math_upstream_server() as math_url:
        config = ProxyConfig(
            server=ServerConfig(),
            local_proxy={"enabled": True, "profile": "default"},
            auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
            profiles={"default": ProfileConfig(upstreams=["echo", "math"])},
            upstreams={
                "echo": UpstreamConfig(transport="stdio", command=sys.executable, args=[str(stdio_server)]),
                "math": UpstreamConfig(url=math_url),
            },
        )

        local_app = create_app(config)
        transport = httpx.ASGITransport(app=local_app)

        async with local_app.router.lifespan_context(local_app):
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as http_client:
                profile_response = await http_client.get("/profiles/default/mcp")
                assert profile_response.status_code == 404

                async with streamable_http_client(
                    "http://127.0.0.1:8080/mcp",
                    http_client=http_client,
                ) as (read_stream, write_stream, _session_id):
                    session = ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=10))
                    async with session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert {tool.name for tool in tools.tools} == {"echo__echo", "math__add"}

                        echo = await session.call_tool("echo__echo", arguments={"value": "ok"})
                        assert echo.isError is False
                        assert echo.content[0].text == "echo:ok"

                        add = await session.call_tool("math__add", arguments={"left": 2, "right": 3})
                        assert add.isError is False
                        assert add.content[0].text == "5"


@asynccontextmanager
async def run_math_upstream_server() -> AsyncIterator[str]:
    manager = StreamableHTTPSessionManager(
        app=build_math_upstream_server(),
        json_response=False,
        stateless=True,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(routes=[Route("/mcp", MathUpstreamApp(manager))], lifespan=lifespan)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            lifespan="on",
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.02)
        if not server.started:
            raise RuntimeError("math upstream server did not start")
        yield f"http://{host}:{port}/mcp"
    finally:
        server.should_exit = True
        await task


class MathUpstreamApp:
    def __init__(self, manager: StreamableHTTPSessionManager):
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.manager.handle_request(scope, receive, send)


def build_math_upstream_server() -> Server:
    server = Server("math-upstream", version="0.1.0")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="add",
                description="Add two integers",
                inputSchema={
                    "type": "object",
                    "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
                    "required": ["left", "right"],
                },
            )
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        if name != "add":
            return CallToolResult(content=[TextContent(type="text", text=f"unknown tool: {name}")], isError=True)
        payload = arguments or {}
        return CallToolResult(
            content=[TextContent(type="text", text=str(int(payload["left"]) + int(payload["right"])))]
        )

    return server


def build_config() -> ProxyConfig:
    return ProxyConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            enabled=True,
            issuer="https://auth.example.test/realms/example",
            dev_bearer_tokens={
                "admin-token": {
                    "sub": "admin",
                    "preferred_username": "nils.friedrichs",
                    "groups": ["/lightnow-proxy-admins"],
                },
                "user-token": {
                    "sub": "user",
                    "preferred_username": "dorothee.warncke",
                    "groups": ["/lightnow-proxy-users"],
                },
            },
        ),
        profiles={
            "users": ProfileConfig(required_groups=["lightnow-proxy-users"], upstreams=["nextcloud"]),
            "admins": ProfileConfig(required_groups=["lightnow-proxy-admins"], upstreams=["grafana", "nextcloud"]),
        },
        upstreams={
            "nextcloud": UpstreamConfig(url="https://nextcloud.example.test/mcp"),
            "grafana": UpstreamConfig(url="https://grafana.example.test/mcp"),
        },
    )


def build_runtime_config() -> ProxyConfig:
    config = build_config()
    return ProxyConfig(
        server=config.server,
        auth=config.auth,
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={
            "admins": ProfileConfig(
                required_groups=["lightnow-proxy-admins"],
                runtime_upstreams=[RuntimeUpstreamConfig(name="grafana", server="grafana", version="1.0.0")],
            )
        },
        upstreams={},
    )
