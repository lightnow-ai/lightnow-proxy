from __future__ import annotations

import asyncio
import json
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest
from mcp.types import CallToolResult, ReadResourceResult, Resource, TextContent, TextResourceContents, Tool

from lightnow_proxy.auth import Principal, TokenVerifier, configured_audiences, has_required_group, normalize_groups
from lightnow_proxy.config import (
    AuthConfig,
    ProfileConfig,
    ProxyConfig,
    RegistryApiConfig,
    RuntimeUpstreamConfig,
    ServerConfig,
    UpstreamConfig,
)
from lightnow_proxy.router import ToolRouter, prefix_resource, prefix_tool, route_resource, runtime_request_metadata


def test_normalize_groups_supports_keycloak_paths() -> None:
    assert normalize_groups(["/lightnow-proxy-admins", "tenant/users"]) == {
        "/lightnow-proxy-admins",
        "lightnow-proxy-admins",
        "tenant/users",
    }


def test_required_group_accepts_leaf_group_name() -> None:
    principal = Principal("sub", "nils.friedrichs", {"/lightnow-proxy-admins", "lightnow-proxy-admins"}, {})
    assert has_required_group(principal, ["lightnow-proxy-admins"])
    assert not has_required_group(principal, ["lightnow-proxy-users"])


def test_required_group_does_not_match_nested_group_leaf_name() -> None:
    principal = Principal("sub", "nils.friedrichs", {"tenant-a/admin", "/tenant-a/admin"}, {})
    assert has_required_group(principal, ["/tenant-a/admin"])
    assert not has_required_group(principal, ["tenant-b/admin"])
    assert not has_required_group(principal, ["admin"])


def test_configured_audiences_normalizes_string_and_list() -> None:
    assert configured_audiences(None) == []
    assert configured_audiences("lightnow-proxy") == ["lightnow-proxy"]
    assert configured_audiences(["legacy-client", "lightnow-proxy"]) == ["legacy-client", "lightnow-proxy"]


def test_token_verifier_accepts_keycloak_authorized_party_without_audience() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    token = jwt.encode(
        {
            "iss": "https://auth.example.test/realms/example",
            "sub": "user-1",
            "azp": "legacy-client",
            "preferred_username": "nils.friedrichs",
            "groups": ["/lightnow-proxy-admins"],
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        algorithm="RS256",
    )
    verifier = TokenVerifier(
        AuthConfig(
            enabled=True,
            issuer="https://auth.example.test/realms/example",
            audience=["legacy-client", "lightnow-proxy"],
        )
    )

    claims = verifier._decode_with_audience_or_authorized_party(token, public_key)

    assert claims["azp"] == "legacy-client"


def test_token_verifier_rejects_unknown_authorized_party_without_audience() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    token = jwt.encode(
        {
            "iss": "https://auth.example.test/realms/example",
            "sub": "user-1",
            "azp": "unknown-client",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        algorithm="RS256",
    )
    verifier = TokenVerifier(
        AuthConfig(
            enabled=True,
            issuer="https://auth.example.test/realms/example",
            audience=["legacy-client", "lightnow-proxy"],
        )
    )

    with pytest.raises(jwt.MissingRequiredClaimError):
        verifier._decode_with_audience_or_authorized_party(token, public_key)


def test_config_rejects_unknown_profile_upstream() -> None:
    config = ProxyConfig(
        server=ServerConfig(),
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        profiles={"admins": ProfileConfig(required_groups=[], upstreams=["missing"])},
        upstreams={},
    )
    with pytest.raises(ValueError, match="unknown upstreams"):
        config.validate_references()


def test_config_accepts_registry_runtime_upstreams_without_static_reference() -> None:
    config = ProxyConfig(
        server=ServerConfig(),
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={
            "admins": ProfileConfig(
                required_groups=[],
                runtime_upstreams=[
                    RuntimeUpstreamConfig(
                        name="grafana",
                        server="grafana",
                        version="1.0.0",
                        runtime_profile="readonly",
                    )
                ],
            )
        },
        upstreams={},
    )

    config.validate_references()
    assert config.profiles["admins"].runtime_upstreams[0].runtime_profile == "readonly"


def test_stdio_upstream_requires_command() -> None:
    with pytest.raises(ValueError, match="stdio upstreams require command"):
        UpstreamConfig(transport="stdio")


def test_streamable_http_upstream_requires_url() -> None:
    with pytest.raises(ValueError, match="streamable-http upstreams require url"):
        UpstreamConfig(transport="streamable-http")


def test_prefix_tool_keeps_original_metadata() -> None:
    tool = Tool(
        name="query_loki_logs",
        description="Query Loki",
        inputSchema={"type": "object", "properties": {"logql": {"type": "string"}}},
    )
    prefixed = prefix_tool("grafana", tool)
    assert prefixed.name == "grafana__query_loki_logs"
    assert prefixed.description == "[grafana] Query Loki"
    assert prefixed.meta["lightnow_proxy_upstream"] == "grafana"
    assert prefixed.meta["lightnow_proxy_original_name"] == "query_loki_logs"


def test_prefix_resource_routes_original_uri() -> None:
    resource = Resource(name="README", uri="file:///workspace/README.md", mimeType="text/markdown")

    prefixed = prefix_resource("filesystem", resource)
    routed = route_resource(str(prefixed.uri))

    assert str(prefixed.uri).startswith("lightnow://filesystem/")
    assert prefixed.meta["lightnow_proxy_upstream"] == "filesystem"
    assert routed.upstream_name == "filesystem"
    assert routed.original_uri == "file:///workspace/README.md"


@pytest.mark.asyncio
async def test_router_rejects_tool_outside_profile() -> None:
    config = ProxyConfig(
        server=ServerConfig(),
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        profiles={"users": ProfileConfig(required_groups=[], upstreams=["nextcloud"])},
        upstreams={
            "nextcloud": UpstreamConfig(url="https://nextcloud.example.test/mcp"),
            "grafana": UpstreamConfig(url="https://grafana.example.test/mcp"),
        },
    )
    router = ToolRouter(config)
    result = await router.call_tool("users", "grafana__query_loki_logs", {})
    assert result.isError is True
    assert isinstance(result.content[0], TextContent)
    assert "not available" in result.content[0].text


@pytest.mark.asyncio
async def test_router_loads_local_proxy_upstreams_from_lightnow_profile() -> None:
    class FakeRegistryClient:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def fetch_profile_upstreams(
            self,
            profile_name: str,
            principal: Principal | None,
        ):
            assert profile_name == "default"
            assert principal is None

            class Resolved:
                name = "grafana"
                config = UpstreamConfig(url="https://grafana.example.test/mcp")
                server_name = "grafana"

            return [Resolved()]

        async def post_runtime_event(self, payload: dict) -> None:
            self.events.append(payload)

    class FakeUpstreamClient:
        async def list_tools(self, config: UpstreamConfig) -> list[Tool]:
            assert str(config.url) == "https://grafana.example.test/mcp"
            return [
                Tool(
                    name="query",
                    description="Query",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]

    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={"enabled": True, "profile": "default", "sync_from_lightnow": True},
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig()},
        upstreams={},
    )
    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    registry_client = FakeRegistryClient()
    router.registry_client = registry_client

    tools = await router.list_tools("default")

    assert [tool.name for tool in tools] == ["grafana__query"]
    assert registry_client.events[0]["event_type"] == "list_tools"
    assert registry_client.events[0]["status"] == "success"


@pytest.mark.asyncio
async def test_router_list_tools_skips_unavailable_upstreams() -> None:
    class FakeRegistryClient:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def fetch_profile_upstreams(
            self,
            profile_name: str,
            principal: Principal | None,
        ):
            assert profile_name == "default"
            assert principal is None

            class Working:
                name = "jenkins"
                config = UpstreamConfig(url="https://jenkins.example.test/mcp")
                server_name = "custom:jenkins"

            class Broken:
                name = "kubernetes"
                config = UpstreamConfig(transport="stdio", command="kubernetes-mcp-server")
                server_name = "io.github.containers/kubernetes-mcp-server"

            return [Working(), Broken()]

        async def post_runtime_event(self, payload: dict) -> None:
            self.events.append(payload)

    class FakeUpstreamClient:
        async def list_tools(self, config: UpstreamConfig) -> list[Tool]:
            if config.transport == "stdio":
                raise RuntimeError("no kube context")
            return [
                Tool(
                    name="getStatus",
                    description="Get status",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]

    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={"enabled": True, "profile": "default", "sync_from_lightnow": True},
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig()},
        upstreams={},
    )
    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    registry_client = FakeRegistryClient()
    router.registry_client = registry_client

    tools = await router.list_tools("default")

    assert [tool.name for tool in tools] == ["jenkins__getStatus"]
    assert registry_client.events[0]["event_type"] == "list_tools"
    assert registry_client.events[0]["status"] == "success"


@pytest.mark.asyncio
async def test_router_lists_upstream_tools_concurrently() -> None:
    class FakeRegistryClient:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def fetch_profile_upstreams(
            self,
            profile_name: str,
            principal: Principal | None,
        ):
            assert profile_name == "default"
            assert principal is None

            class First:
                name = "first"
                config = UpstreamConfig(url="https://first.example.test/mcp")
                server_name = "custom:first"

            class Second:
                name = "second"
                config = UpstreamConfig(url="https://second.example.test/mcp")
                server_name = "custom:second"

            return [First(), Second()]

        async def post_runtime_event(self, payload: dict) -> None:
            self.events.append(payload)

    class FakeUpstreamClient:
        async def list_tools(self, config: UpstreamConfig) -> list[Tool]:
            await asyncio.sleep(0.05)
            return [
                Tool(
                    name="query",
                    description=f"Query {config.url.host}",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]

    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={"enabled": True, "profile": "default", "sync_from_lightnow": True},
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig()},
        upstreams={},
    )
    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    registry_client = FakeRegistryClient()
    router.registry_client = registry_client

    started = time.monotonic()
    tools = await router.list_tools("default")
    elapsed = time.monotonic() - started

    assert [tool.name for tool in tools] == ["first__query", "second__query"]
    assert elapsed < 0.09
    assert registry_client.events[0]["event_type"] == "list_tools"
    assert registry_client.events[0]["status"] == "success"


@pytest.mark.asyncio
async def test_router_uses_fresh_persistent_tool_cache(tmp_path) -> None:
    class FakeRegistryClient:
        async def fetch_profile_upstreams(
            self,
            profile_name: str,
            principal: Principal | None,
        ):
            raise AssertionError("fresh tools cache should avoid upstream discovery")

        async def post_runtime_event(self, payload: dict) -> None:
            pass

    cache_path = tmp_path / "tools.json"
    cached_tool = Tool(name="jenkins__getStatus", description="Get status", inputSchema={"type": "object"})
    cache_path.write_text(
        json.dumps(
            {
                "generated_at": time.time(),
                "tools": [cached_tool.model_dump(exclude_none=True)],
            }
        )
    )
    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={
            "enabled": True,
            "profile": "default",
            "sync_from_lightnow": True,
            "tool_cache_path": str(cache_path),
        },
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig()},
        upstreams={},
    )
    router = ToolRouter(config)
    router.registry_client = FakeRegistryClient()

    tools = await router.list_tools("default")

    assert [tool.name for tool in tools] == ["jenkins__getStatus"]


@pytest.mark.asyncio
async def test_router_resolves_only_target_lightnow_upstream_for_tool_calls() -> None:
    class FakeRegistryClient:
        def __init__(self) -> None:
            self.events: list[dict] = []
            self.fetch_all_called = False

        async def fetch_profile_upstream_names(self, profile_name: str) -> list[str]:
            assert profile_name == "default"
            return ["grafana", "jenkins"]

        async def fetch_profile_upstream(
            self,
            profile_name: str,
            alias: str,
            principal: Principal | None,
        ):
            assert profile_name == "default"
            assert alias == "jenkins"
            assert principal is None

            class Resolved:
                name = "jenkins"
                config = UpstreamConfig(url="https://jenkins.example.test/mcp")
                server_name = "custom:jenkins"

            return Resolved()

        async def fetch_profile_upstreams(
            self,
            profile_name: str,
            principal: Principal | None,
        ):
            self.fetch_all_called = True
            raise AssertionError("call_tool must not resolve every LightNow upstream")

        async def post_runtime_event(self, payload: dict) -> None:
            self.events.append(payload)

    class FakeUpstreamClient:
        async def call_tool(self, config: UpstreamConfig, tool_name: str, arguments: dict | None):
            assert str(config.url) == "https://jenkins.example.test/mcp"
            assert tool_name == "getStatus"
            from mcp.types import CallToolResult, TextContent

            return CallToolResult(content=[TextContent(type="text", text="ok")])

    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={"enabled": True, "profile": "default", "sync_from_lightnow": True},
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig()},
        upstreams={},
    )
    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    registry_client = FakeRegistryClient()
    router.registry_client = registry_client

    result = await router.call_tool("default", "jenkins__getStatus", {})

    assert result.isError is not True
    assert registry_client.fetch_all_called is False
    assert registry_client.events[0]["server_alias"] == "jenkins"
    assert registry_client.events[0]["server_name"] == "custom:jenkins"


@pytest.mark.asyncio
async def test_router_emits_call_tool_event_without_arguments() -> None:
    class FakeRegistryClient:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def post_runtime_event(self, payload: dict) -> None:
            self.events.append(payload)

    class FakeUpstreamClient:
        async def call_tool(self, config: UpstreamConfig, tool_name: str, arguments: dict | None):
            assert str(config.url) == "https://grafana.example.test/mcp"
            assert tool_name == "query"
            assert arguments == {"token": "must-stay-local", "query": "up"}
            from mcp.types import CallToolResult, TextContent

            return CallToolResult(content=[TextContent(type="text", text="ok")])

    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={"enabled": True, "profile": "default", "client_name": "codex"},
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig(required_groups=[], upstreams=["grafana"])},
        upstreams={"grafana": UpstreamConfig(url="https://grafana.example.test/mcp")},
    )
    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    registry_client = FakeRegistryClient()
    router.registry_client = registry_client

    result = await router.call_tool(
        "default",
        "grafana__query",
        {"token": "must-stay-local", "query": "up"},
        {
            "threadId": "thread-1",
            "progressToken": "progress-1",
            "x-codex-turn-metadata": {
                "session_id": "session-1",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "sandbox": "none",
                "workspaces": {
                    "/workspace/lightnow": {
                        "associated_remote_urls": {"origin": "git@github.com:example/private-repo.git"},
                        "has_changes": True,
                        "latest_git_commit_hash": "abc123",
                    }
                },
            },
        },
    )

    assert result.isError is not True
    event = registry_client.events[0]
    assert event["profile"] == "default"
    assert event["event_type"] == "call_tool"
    assert event["status"] == "success"
    assert event["server_alias"] == "grafana"
    assert event["tool_name"] == "grafana__query"
    assert event["original_tool_name"] == "query"
    assert event["client_name"] == "codex"
    assert event["mcp_client_name"] == "codex"
    assert event["runner_name"] == "lightnow-local-proxy"
    assert event["mcp_method"] == "tools/call"
    assert event["upstream_transport"] == "streamable-http"
    assert event["argument_keys"] == ["query", "token"]
    assert event["request_meta_keys"] == ["progressToken", "threadId", "x-codex-turn-metadata"]
    assert event["mcp_thread_id"] == "thread-1"
    assert event["client_context_session_id"] == "session-1"
    assert event["client_context_thread_id"] == "thread-1"
    assert event["client_context_turn_id"] == "turn-1"
    assert event["client_context_model"] == "gpt-5.5"
    assert event["client_context_reasoning"] == "medium"
    assert event["client_context_sandbox"] == "none"
    assert event["client_context_metadata"] == {"adapter": "codex"}
    assert event["codex_session_id"] == "session-1"
    assert event["codex_thread_id"] == "thread-1"
    assert event["codex_turn_id"] == "turn-1"
    assert event["codex_model"] == "gpt-5.5"
    assert event["codex_reasoning_effort"] == "medium"
    assert event["codex_sandbox"] == "none"
    assert "codex_workspace_path" not in event
    assert "codex_workspace_has_changes" not in event
    assert "codex_workspace_commit_hash" not in event
    assert "codex_workspace_remote_url" not in event
    assert event["request_bytes"] > 0
    assert event["response_items_count"] == 1
    assert event["response_bytes"] > 0
    assert "arguments" not in registry_client.events[0]
    assert "must-stay-local" not in str(registry_client.events[0])


@pytest.mark.asyncio
async def test_router_maps_antigravity_conversation_to_generic_client_context() -> None:
    class FakeRegistryClient:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        async def post_runtime_event(self, payload: dict[str, object]) -> None:
            self.events.append(payload)

    class FakeUpstreamClient:
        async def call_tool(self, config: UpstreamConfig, tool_name: str, arguments: dict[str, object]) -> CallToolResult:
            return CallToolResult(content=[TextContent(type="text", text="ok")], isError=False)

    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={"enabled": True, "profile": "default", "client_name": "antigravity"},
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig(required_groups=[], upstreams=["jenkins"])},
        upstreams={"jenkins": UpstreamConfig(url="https://jenkins.example.test/mcp")},
    )
    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    router.update_client_context(name="antigravity-client", version="v1.0.0", capability_keys=["elicitation"])
    registry_client = FakeRegistryClient()
    router.registry_client = registry_client

    await router.call_tool(
        "default",
        "jenkins__whoAmI",
        {},
        {
            "antigravity.google/artifacts_dir": "/Users/nils/.gemini/antigravity/brain/private",
            "antigravity.google/conversation_id": "36326ddf-b460-4e49-9015-f796dba21110",
            "progressToken": "must-not-be-stored",
        },
    )

    event = registry_client.events[0]
    assert event["client_name"] == "antigravity-client"
    assert event["mcp_client_name"] == "antigravity-client"
    assert event["mcp_client_version"] == "v1.0.0"
    assert event["request_meta_keys"] == [
        "antigravity.google/artifacts_dir",
        "antigravity.google/conversation_id",
        "progressToken",
    ]
    assert event["mcp_thread_id"] == "36326ddf-b460-4e49-9015-f796dba21110"
    assert event["client_context_thread_id"] == "36326ddf-b460-4e49-9015-f796dba21110"
    assert event["client_context_metadata"] == {"adapter": "antigravity", "progress_reported": True}
    assert "artifacts_dir" not in str(event["client_context_metadata"])
    assert "/Users/nils" not in str(event)
    assert "must-not-be-stored" not in str(event)
    assert "codex_thread_id" not in event
    assert "client_context_turn_id" not in event


def test_antigravity_context_does_not_override_standard_mcp_thread_id() -> None:
    metadata = runtime_request_metadata(
        {},
        {
            "threadId": "mcp-thread",
            "antigravity.google/conversation_id": "antigravity-conversation",
        },
    )

    assert metadata["mcp_thread_id"] == "mcp-thread"
    assert metadata["client_context_thread_id"] == "antigravity-conversation"


@pytest.mark.asyncio
async def test_router_uses_initialize_client_version_for_runtime_events() -> None:
    class FakeRegistryClient:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        async def post_runtime_event(self, payload: dict[str, object]) -> None:
            self.events.append(payload)

    class FakeUpstreamClient:
        async def list_tools(self, config: UpstreamConfig) -> list[Tool]:
            assert str(config.url) == "https://jenkins.example.test/mcp"
            return [Tool(name="getJobs", inputSchema={})]

    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={"enabled": True, "profile": "default", "client_name": "claude-desktop"},
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig(required_groups=[], upstreams=["jenkins"])},
        upstreams={"jenkins": UpstreamConfig(url="https://jenkins.example.test/mcp")},
    )
    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    router.update_client_context(name="Claude Desktop", version="0.12.3", capability_keys=["roots"])
    registry_client = FakeRegistryClient()
    router.registry_client = registry_client

    await router.list_tools("default")

    event = registry_client.events[0]
    assert event["client_name"] == "Claude Desktop"
    assert event["mcp_client_name"] == "Claude Desktop"
    assert event["mcp_client_version"] == "0.12.3"
    assert "codex_model" not in event
    assert "codex_turn_id" not in event


@pytest.mark.asyncio
async def test_router_lists_and_reads_resources_without_contents_in_events() -> None:
    class FakeRegistryClient:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def post_runtime_event(self, payload: dict) -> None:
            self.events.append(payload)

    class FakeUpstreamClient:
        async def list_resources(self, config: UpstreamConfig) -> list[Resource]:
            assert str(config.url) == "https://docs.example.test/mcp"
            return [Resource(name="README", uri="file:///repo/README.md", mimeType="text/markdown")]

        async def read_resource(self, config: UpstreamConfig, uri: str) -> ReadResourceResult:
            assert str(config.url) == "https://docs.example.test/mcp"
            assert uri == "file:///repo/README.md"
            return ReadResourceResult(
                contents=[
                    TextResourceContents(uri="file:///repo/README.md", mimeType="text/markdown", text="must-stay-local")
                ]
            )

    config = ProxyConfig(
        server=ServerConfig(),
        local_proxy={"enabled": True, "profile": "default", "client_name": "codex"},
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test/realms/example"),
        registry_api=RegistryApiConfig(enabled=True, base_url="https://registry-api.example.test"),
        profiles={"default": ProfileConfig(required_groups=[], upstreams=["docs"])},
        upstreams={"docs": UpstreamConfig(url="https://docs.example.test/mcp")},
    )
    router = ToolRouter(config, upstream_client=FakeUpstreamClient())
    registry_client = FakeRegistryClient()
    router.registry_client = registry_client

    resources = await router.list_resources("default")
    result = await router.read_resource("default", str(resources[0].uri))

    assert result.contents[0].text == "must-stay-local"
    assert registry_client.events[0]["event_type"] == "list_resources"
    assert registry_client.events[0]["response_items_count"] == 1
    assert registry_client.events[1]["event_type"] == "read_resource"
    assert registry_client.events[1]["resource_uri"] == str(resources[0].uri)
    assert registry_client.events[1]["response_content_type"] == "text/markdown"
    assert registry_client.events[1]["response_bytes"] > 0
    assert "must-stay-local" not in str(registry_client.events)
