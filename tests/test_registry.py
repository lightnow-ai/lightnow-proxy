from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from lightnow_proxy.config import RegistryApiConfig, RuntimeSecretsConfig, RuntimeUpstreamConfig
from lightnow_proxy.registry import (
    CliSession,
    RegistryApiClient,
    RegistryApiError,
    upstream_config_from_client_config,
    upstream_config_from_runtime_context,
)
from lightnow_proxy.runtime_secrets import RuntimeSecretResolver


def test_runtime_http_context_becomes_streamable_http_upstream() -> None:
    upstream = RuntimeUpstreamConfig(name="grafana", server="grafana", version="1.0.0")
    config = upstream_config_from_runtime_context(
        {
            "probe_request": {
                "transport": "http",
                "http": {
                    "url": "https://grafana.example.test/mcp",
                    "headers": {"Authorization": "Bearer token"},
                },
            }
        },
        upstream,
    )

    assert config.transport == "streamable-http"
    assert str(config.url) == "https://grafana.example.test/mcp"
    assert config.headers == {"Authorization": "Bearer token"}


def test_runtime_stdio_context_becomes_stdio_upstream() -> None:
    upstream = RuntimeUpstreamConfig(name="temporal", server="temporal", version="1.0.0", timeout_seconds=5)
    config = upstream_config_from_runtime_context(
        {
            "probe_request": {
                "transport": "stdio",
                "stdio": {
                    "cmd": "uvx",
                    "args": ["temporal-mcp"],
                    "env": {"TEMPORAL_NAMESPACE": "default"},
                },
            }
        },
        upstream,
    )

    assert config.transport == "stdio"
    assert config.command == "uvx"
    assert config.args == ["temporal-mcp"]
    assert config.env == {"TEMPORAL_NAMESPACE": "default"}
    assert config.timeout_seconds == 5


def test_custom_stdio_client_config_becomes_stdio_upstream() -> None:
    config = upstream_config_from_client_config(
        {
            "transport": "stdio",
            "command": "uvx",
            "args": ["redis-mcp-server"],
            "env": {"REDIS_HOST": "localhost"},
            "cwd": "/tmp",
            "tool_timeout_sec": 7,
        }
    )

    assert config.transport == "stdio"
    assert config.command == "uvx"
    assert config.args == ["redis-mcp-server"]
    assert config.env == {"REDIS_HOST": "localhost"}
    assert config.cwd == "/tmp"
    assert config.timeout_seconds == 7


def test_custom_http_client_config_becomes_streamable_http_upstream() -> None:
    config = upstream_config_from_client_config(
        {
            "transport": "http",
            "url": "https://jenkins.example.test/mcp",
            "headers": {"Authorization": "Bearer token"},
        }
    )

    assert config.transport == "streamable-http"
    assert str(config.url) == "https://jenkins.example.test/mcp"
    assert config.headers == {"Authorization": "Bearer token"}


def test_runtime_sse_context_is_rejected_for_now() -> None:
    upstream = RuntimeUpstreamConfig(name="legacy", server="legacy", version="1.0.0")
    with pytest.raises(RegistryApiError, match="SSE"):
        upstream_config_from_runtime_context({"probe_request": {"transport": "sse"}}, upstream)


def unsigned_token(exp: int | None = None) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {"sub": "user-1"}
    if exp is not None:
        payload["exp"] = exp

    import base64

    def encode(data: dict[str, object]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode(header)}.{encode(payload)}."


def test_cli_session_loads_tenant_context(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "refresh_token": "refresh",
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
                "context_type": "tenant",
                "context_tenant": "tenant-uuid",
            }
        )
    )

    session = CliSession.load(config_path)

    assert session.tenant_id == "tenant-uuid"
    assert session.access_token_expired() is False


@pytest.mark.asyncio
async def test_registry_client_fetches_profile_servers_with_cli_session(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
                "context_type": "tenant",
                "context_tenant": "tenant-uuid",
            }
        )
    )
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local/v0.1",
            use_cli_session=True,
            cli_config_path=str(config_path),
        )
    )

    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://registry-api.lightnow.local/v0.1/integrations/profiles/default/servers").respond(
            200,
            json={
                "servers": [
                    {
                        "alias": "release-smoke",
                        "server_name": "codex-test1.lightnow/release-smoke",
                        "version": "1.0.1",
                        "status": "linked",
                    }
                ]
            },
        )

        upstreams = await client.fetch_profile_runtime_upstreams("default")

    assert route.calls.last.request.headers["Authorization"].startswith("Bearer ")
    assert route.calls.last.request.headers["X-Tenant"] == "tenant-uuid"
    assert upstreams == [
        RuntimeUpstreamConfig(
            name="release-smoke",
            server="codex-test1.lightnow/release-smoke",
            version="1.0.1",
            runtime_profile="default",
        )
    ]


@pytest.mark.asyncio
async def test_registry_client_uses_configured_ca_file_for_registry_requests(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    ca_file = tmp_path / "lightnow-local-ca.crt"
    ca_file.write_text("certificate")
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
            }
        )
    )
    verifies: list[str | bool] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"servers": []}

    class RecordingAsyncClient:
        def __init__(self, *args, **kwargs):
            verifies.append(kwargs.get("verify", True))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", RecordingAsyncClient)
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local/v0.1",
            ca_file=str(ca_file),
            use_cli_session=True,
            cli_config_path=str(config_path),
        )
    )

    await client.fetch_profile_runtime_upstreams("default")

    assert verifies == [str(ca_file)]


@pytest.mark.asyncio
async def test_registry_client_fetches_mixed_profile_upstreams(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
                "context_type": "tenant",
                "context_tenant": "tenant-uuid",
            }
        )
    )
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local/v0.1",
            use_cli_session=True,
            cli_config_path=str(config_path),
        )
    )

    with respx.mock(assert_all_called=True) as router:
        router.get("https://registry-api.lightnow.local/v0.1/integrations/profiles/default/servers").respond(
            200,
            json={
                "servers": [
                    {
                        "alias": "redis-test",
                        "server_name": "custom:redis-test",
                        "status": "custom",
                        "client_config": {
                            "transport": "stdio",
                            "command": "uvx",
                            "args": ["redis-mcp-server"],
                            "env": {"REDIS_HOST": "localhost"},
                        },
                    },
                    {
                        "alias": "release-smoke",
                        "server_name": "codex-test1.lightnow/release-smoke",
                        "version": "1.0.1",
                        "status": "linked",
                    },
                ]
            },
        )
        router.get(
            "https://registry-api.lightnow.local/v0.1/servers/codex-test1.lightnow%2Frelease-smoke/versions/1.0.1/context"
        ).respond(
            200,
            json={
                "probe_request": {
                    "transport": "stdio",
                    "stdio": {
                        "cmd": "uvx",
                        "args": ["release-smoke"],
                    },
                }
            },
        )

        upstreams = await client.fetch_profile_upstreams("default", None)

    assert [upstream.name for upstream in upstreams] == ["redis-test", "release-smoke"]
    assert upstreams[0].server_name == "custom:redis-test"
    assert upstreams[0].config.command == "uvx"
    assert upstreams[0].config.env == {"REDIS_HOST": "localhost"}
    assert upstreams[1].server_name == "codex-test1.lightnow/release-smoke"
    assert upstreams[1].config.command == "uvx"


@pytest.mark.asyncio
async def test_registry_client_fetches_profile_names_without_secrets(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
            }
        )
    )
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local/v0.1",
            use_cli_session=True,
            cli_config_path=str(config_path),
        )
    )

    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://registry-api.lightnow.local/v0.1/integrations/profiles/default/servers").respond(
            200,
            json={
                "servers": [
                    {"alias": "grafana", "server_name": "custom:grafana", "status": "custom"},
                    {"alias": "jenkins", "server_name": "custom:jenkins", "status": "custom"},
                ]
            },
        )

        names = await client.fetch_profile_upstream_names("default")

    assert dict(route.calls.last.request.url.params) == {}
    assert names == ["grafana", "jenkins"]


@pytest.mark.asyncio
async def test_registry_client_fetches_one_profile_upstream_with_secrets(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
            }
        )
    )
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local/v0.1",
            use_cli_session=True,
            cli_config_path=str(config_path),
        )
    )

    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://registry-api.lightnow.local/v0.1/integrations/profiles/default/servers").respond(
            200,
            json={
                "servers": [
                    {
                        "alias": "grafana",
                        "server_name": "custom:grafana",
                        "status": "custom",
                        "client_config": {
                            "transport": "stdio",
                            "command": "docker",
                            "env": {"TOKEN": "${GRAFANA_TOKEN}"},
                        },
                    },
                    {
                        "alias": "jenkins",
                        "server_name": "custom:jenkins",
                        "status": "custom",
                        "client_config": {
                            "transport": "http",
                            "url": "https://jenkins.example.test/mcp",
                            "headers": {"Authorization": "Bearer token"},
                        },
                    },
                ]
            },
        )

        upstream = await client.fetch_profile_upstream("default", "jenkins", None)

    assert dict(route.calls.last.request.url.params) == {"include": "secrets"}
    assert upstream.name == "jenkins"
    assert upstream.server_name == "custom:jenkins"
    assert str(upstream.config.url) == "https://jenkins.example.test/mcp"
    assert upstream.config.headers == {"Authorization": "Bearer token"}


@pytest.mark.asyncio
async def test_registry_client_prefers_configured_tenant_over_cli_context(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
                "context_type": "tenant",
                "context_tenant": "cli-tenant",
            }
        )
    )
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local/v0.1",
            use_cli_session=True,
            cli_config_path=str(config_path),
            cli_tenant_id="configured-tenant",
        )
    )

    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://registry-api.lightnow.local/v0.1/integrations/profiles/default/servers").respond(
            200, json={"servers": []}
        )

        await client.fetch_profile_runtime_upstreams("default")

    assert route.calls.last.request.headers["X-Tenant"] == "configured-tenant"


@pytest.mark.asyncio
async def test_registry_client_posts_runtime_events_with_cli_session(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
                "context_type": "tenant",
                "context_tenant": "tenant-uuid",
            }
        )
    )
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local/v0.1",
            use_cli_session=True,
            cli_config_path=str(config_path),
        )
    )

    payload = {
        "profile": "default",
        "event_type": "call_tool",
        "status": "success",
        "server_alias": "github",
        "tool_name": "github__search_repositories",
    }
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://registry-api.lightnow.local/v0.1/integrations/runtime-events").respond(
            201,
            json={"event": payload},
        )

        await client.post_runtime_event(payload)

    assert route.calls.last.request.headers["Authorization"].startswith("Bearer ")
    assert route.calls.last.request.headers["X-Tenant"] == "tenant-uuid"
    assert json.loads(route.calls.last.request.content) == payload


@pytest.mark.asyncio
async def test_registry_client_resolves_runtime_context_with_local_runner_consumer(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
                "context_type": "personal",
            }
        )
    )
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local",
            use_cli_session=True,
            cli_config_path=str(config_path),
        )
    )
    upstream = RuntimeUpstreamConfig(
        name="release-smoke",
        server="codex-test1.lightnow/release-smoke",
        version="1.0.1",
        runtime_profile="default",
    )

    with respx.mock(assert_all_called=True) as router:
        route = router.get(
            "https://registry-api.lightnow.local/v0.1/servers/codex-test1.lightnow%2Frelease-smoke/versions/1.0.1/context"
        ).respond(
            200,
            json={
                "probe_request": {
                    "transport": "stdio",
                    "stdio": {"cmd": "npx", "args": ["-y", "@lightnow/release-smoke-mcp"]},
                }
            },
        )

        resolved = await client.resolve_upstream(upstream, None)

    assert route.calls.last.request.url.params["profile"] == "default"
    assert route.calls.last.request.url.params["include"] == "secrets"
    assert route.calls.last.request.url.params["consumer"] == "local-runner"
    assert "scopeType" not in route.calls.last.request.url.params
    assert resolved.name == "release-smoke"
    assert resolved.config.transport == "stdio"


@pytest.mark.asyncio
async def test_registry_client_resolves_external_bindings_before_building_upstream(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
                "context_type": "personal",
            }
        )
    )
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local",
            use_cli_session=True,
            cli_config_path=str(config_path),
        ),
        RuntimeSecretResolver(RuntimeSecretsConfig()),
    )
    upstream = RuntimeUpstreamConfig(
        name="release-smoke",
        server="codex-test1.lightnow/release-smoke",
        version="1.0.1",
    )

    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://registry-api.lightnow.local/v0.1/servers/codex-test1.lightnow%2Frelease-smoke/versions/1.0.1/context"
        ).respond(
            200,
            json={
                "probe_request": {"transport": "stdio", "stdio": {"cmd": "npx", "args": []}},
                "external_secret_bindings": [
                    {
                        "provider": {
                            "id": "provider-uuid",
                            "provider_type": "vault_kv_v2",
                            "resolution_mode": "runtime",
                            "config": {"address": "https://vault.internal"},
                        },
                        "locator": {"path": "secret/data/lightnow/test", "field": "token"},
                        "target": {"type": "env", "name": "API_TOKEN"},
                    }
                ],
            },
        )
        router.get("http://127.0.0.1:8200/v1/secret/data/lightnow/test").respond(
            200, json={"data": {"data": {"token": "runtime-value"}}}
        )

        resolved = await client.resolve_upstream(upstream, None)

    assert resolved.config.env == {"API_TOKEN": "runtime-value"}


@pytest.mark.asyncio
async def test_registry_client_omits_include_when_secrets_are_disabled(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) + 3600),
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
                "context_type": "personal",
            }
        )
    )
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local",
            include_secrets=False,
            use_cli_session=True,
            cli_config_path=str(config_path),
        )
    )
    upstream = RuntimeUpstreamConfig(name="surf", server="tech.geckovision/surf", version="0.2.0")

    with respx.mock(assert_all_called=True) as router:
        route = router.get(
            "https://registry-api.lightnow.local/v0.1/servers/tech.geckovision%2Fsurf/versions/0.2.0/context"
        ).respond(
            200,
            json={
                "probe_request": {
                    "transport": "http",
                    "http": {"url": "https://mcp.geckovision.tech/mcp"},
                }
            },
        )

        await client.resolve_upstream(upstream, None)

    assert "include" not in route.calls.last.request.url.params
    assert route.calls.last.request.url.params["consumer"] == "local-runner"


@pytest.mark.asyncio
async def test_registry_client_refreshes_expired_cli_session(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "access_token": unsigned_token(int(time.time()) - 60),
                "refresh_token": "refresh-old",
                "issuer": "https://auth.lightnow.local/realms/lightnow-local",
                "client_id": "lightnow-cli",
                "context_type": "personal",
            }
        )
    )
    refreshed = unsigned_token(int(time.time()) + 3600)
    client = RegistryApiClient(
        RegistryApiConfig(
            enabled=True,
            base_url="https://registry-api.lightnow.local",
            use_cli_session=True,
            cli_config_path=str(config_path),
        )
    )

    with respx.mock(assert_all_called=True) as router:
        router.post("https://auth.lightnow.local/realms/lightnow-local/protocol/openid-connect/token").respond(
            200,
            json={"access_token": refreshed, "refresh_token": "refresh-new"},
        )
        profile_route = router.get(
            "https://registry-api.lightnow.local/v0.1/integrations/profiles/default/servers"
        ).respond(200, json={"servers": []})

        await client.fetch_profile_runtime_upstreams("default")

    assert profile_route.calls.last.request.headers["Authorization"] == f"Bearer {refreshed}"
    saved = json.loads(config_path.read_text())
    assert saved["access_token"] == refreshed
    assert saved["refresh_token"] == "refresh-new"
