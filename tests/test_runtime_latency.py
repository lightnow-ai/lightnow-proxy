"""Latency regressions: no optional network wait and no cached authority."""
import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from mcp.types import CallToolResult, TextContent

from lightnow_proxy.config import AuthConfig, ProfileConfig, ProxyConfig, RegistryApiConfig, UpstreamConfig
from lightnow_proxy.router import ToolRouter, unwrap_error
from test_managed_runtime import ASSIGNMENT, CLIENT, CONNECTION, REGISTRY, TOOL_PREFIX, managed_context, managed_router


def telemetry_router(registry):
    config = ProxyConfig(auth=AuthConfig(enabled=False, issuer="https://issuer.test"), profiles={"default": ProfileConfig()},
                         registry_api=RegistryApiConfig(enabled=True, base_url="https://registry.test"), upstreams={})
    router = ToolRouter(config)
    router.registry_client = registry
    return router


@pytest.mark.asyncio
async def test_slow_optional_delivery_does_not_hold_business_result():
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    class Registry:
        async def post_runtime_event(self, event):
            entered.set()
            await release.wait()
            delivered.append(event)

    router = telemetry_router(Registry())
    router.config.profiles["default"].upstreams = ["notes"]
    router.config.upstreams["notes"] = UpstreamConfig(url="https://notes.test/mcp")

    class Upstream:
        async def call_tool(self, *args):
            return CallToolResult(content=[TextContent(type="text", text="stored")])

    router.upstream_client = Upstream()
    result = await asyncio.wait_for(router.call_tool("default", "notes__write", {}), .5)
    assert result.content[0].text == "stored"
    await asyncio.wait_for(entered.wait(), .5)
    assert delivered == []  # The response exists while the request is blocked.
    release.set()
    await router.close_runtime_events()
    assert len(delivered) == 1
    assert router.runtime_telemetry_status()["sent"] == 1


@pytest.mark.asyncio
async def test_optional_delivery_failures_do_not_stop_next_event_or_leak_error(caplog):
    events = []

    class Registry:
        async def post_runtime_event(self, event):
            if not events:
                events.append(event)
                raise RuntimeError("PRIVATE_URL_AND_TOKEN")
            events.append(event)

    router = telemetry_router(Registry())
    await router._emit_runtime_event({"event_type": "first"})
    await router._emit_runtime_event({"event_type": "second"})
    await router.close_runtime_events()
    assert [e["event_type"] for e in events] == ["first", "second"]
    assert router.runtime_telemetry_status()["failed"] == 1
    assert router.runtime_telemetry_status()["sent"] == 1
    assert "PRIVATE_URL_AND_TOKEN" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [False, True])
async def test_optional_buffer_is_bounded_and_loss_is_visible(oversized):
    class Registry:
        async def post_runtime_event(self, event):
            pass

    router = telemetry_router(Registry())
    count = 1 if oversized else 130
    for _ in range(count):
        await router._emit_runtime_event({"event_type": "test", "value": "x" * (1024 * 1024 if oversized else 1)})
    status = router.runtime_telemetry_status()
    assert status["pending"] == (0 if oversized else 128)
    assert status["pending_bytes"] <= 1024 * 1024
    assert status["dropped"] == (1 if oversized else 2)
    await router.close_runtime_events()
    assert router.runtime_telemetry_status()["pending_bytes"] == 0


@pytest.mark.asyncio
async def test_shutdown_deadline_cancels_slow_sender_and_accounts_for_every_event(monkeypatch):
    started = asyncio.Event()

    class Registry:
        async def post_runtime_event(self, event):
            started.set()
            await asyncio.Event().wait()

    router = telemetry_router(Registry())
    await router._emit_runtime_event({"event_type": "first"})
    await router._emit_runtime_event({"event_type": "second"})
    await started.wait()
    monkeypatch.setattr("lightnow_proxy.router.TELEMETRY_DRAIN_SECONDS", .01)
    await asyncio.wait_for(router.close_runtime_events(), .5)
    assert router._telemetry_task.done()
    assert router.runtime_telemetry_status() == {"pending": 0, "pending_bytes": 0, "sent": 0, "failed": 0, "dropped": 2}


@pytest.mark.asyncio
async def test_queued_event_is_an_immutable_snapshot_and_worker_restarts():
    events = []

    class Registry:
        async def post_runtime_event(self, event):
            events.append(event)

    router = telemetry_router(Registry())
    original = {"event_type": "first", "argument_keys": ["original"]}
    await router._emit_runtime_event(original)
    original["argument_keys"].append("later")
    await router.flush_runtime_events()
    await router._emit_runtime_event({"event_type": "second"})
    await router.close_runtime_events()
    assert events[0]["argument_keys"] == ["original"]
    assert [e["event_type"] for e in events] == ["first", "second"]


@pytest.mark.asyncio
async def test_fast_legacy_lookup_still_reads_new_configuration_and_access_every_time(tmp_path):
    router = managed_router(tmp_path)
    router.config.local_proxy.telemetry_enabled = False
    state = {"host": "first", "access": True}
    calls = []

    def profile(request):
        calls.append(request)
        if not state["access"]:
            return httpx.Response(403)
        return httpx.Response(200, json={"organization_admission": {"required": False}, "servers": [{
            "alias": "docs", "server_name": "custom:docs", "status": "custom",
            "client_config": {"transport": "http", "url": f'https://{state["host"]}.test/mcp'},
        }]})

    with respx.mock(assert_all_called=False) as api:
        api.get(REGISTRY + "/integrations/profiles/default/servers").mock(side_effect=profile)
        _, first = await router._call_target("default", "docs__read")
        assert len(calls) == 2
        state["host"] = "second"
        _, second = await router._call_target("default", "docs__read")
        assert len(calls) == 3
        assert second.config.url != first.config.url
        assert str(second.config.url) == "https://second.test/mcp"
        assert calls[-1].url.params.get("alias") == "docs"
        assert calls[-1].url.params.get("include") == "secrets"
        state["access"] = False
        with pytest.raises(httpx.HTTPStatusError):
            await router._call_target("default", "docs__read")
        assert len(calls) == 4


@pytest.mark.asyncio
async def test_fast_lookup_cannot_preserve_legacy_bypass_when_admission_changes(tmp_path):
    router = managed_router(tmp_path)
    state = {"required": False}

    def profile(request):
        item = {"alias": "docs", "server_name": "custom:docs", "status": "custom",
                "client_config": {"transport": "http", "url": "https://docs.test/mcp"}}
        if state["required"]:
            item.update(status="admission_required", client_config=None)
        return httpx.Response(200, json={"organization_admission": {"required": state["required"]}, "servers": [item]})

    with respx.mock(assert_all_called=False) as api:
        api.get(REGISTRY + "/integrations/profiles/default/servers").mock(side_effect=profile)
        await router._call_target("default", "docs__read")
        state["required"] = True
        with pytest.raises(Exception):
            await router._call_target("default", "docs__read")


@pytest.mark.asyncio
async def test_managed_hint_is_not_authority_through_rename_reuse_and_removal(tmp_path):
    router = managed_router(tmp_path)
    router.registry_client.runtime_client_id = CLIENT
    state = {"alias": "docs", "assignment": ASSIGNMENT}
    requests = []

    def profile(request):
        requests.append(request)
        alias = request.url.params.get("alias")
        rows = []
        if alias in (None, state["alias"]):
            context = {**managed_context(), "assignment_id": state["assignment"]}
            rows.append({"alias": state["alias"], "assignment_id": state["assignment"], "connection_id": CONNECTION,
                         "status": "linked", "managed": context, "client_config": {"transport": "http", "url": "https://docs.test/mcp"}})
        return httpx.Response(200, json={"organization_admission": {"required": True}, "servers": rows})

    with respx.mock(assert_all_called=False) as api:
        api.get(REGISTRY + "/integrations/profiles/default/servers").mock(side_effect=profile)
        await router._call_target("default", TOOL_PREFIX + "__read")
        before = len(requests)
        await router._call_target("default", TOOL_PREFIX + "__read")
        assert len(requests) - before == 1
        state["alias"] = "renamed"
        _, current = await router._call_target("default", TOOL_PREFIX + "__read")
        assert current.managed.tool_namespace == TOOL_PREFIX
        assert requests[-1].url.params.get("alias") == "renamed"
        state["assignment"] = str(uuid4())
        with pytest.raises(Exception) as removed:
            await router._call_target("default", TOOL_PREFIX + "__read")
        assert str(unwrap_error(removed.value)) == "MANAGED_ASSIGNMENT_UNAVAILABLE"
        assert "ln_" + UUID(state["assignment"]).hex != TOOL_PREFIX


@pytest.mark.asyncio
async def test_unserializable_optional_event_never_fails_business_path():
    class Registry:
        async def post_runtime_event(self, event):
            raise AssertionError("invalid event must not be sent")

    router = telemetry_router(Registry())
    await router._emit_runtime_event({"event_type": "test", "bad": object()})
    await router.close_runtime_events()
    assert router.runtime_telemetry_status()["dropped"] == 1
    assert router.runtime_telemetry_status()["pending"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("long_alias,wire", [("docs_", "docs___read"), ("docs__inner", "docs__inner__read")])
async def test_legacy_hint_cannot_capture_a_longer_current_alias(tmp_path, long_alias, wire):
    router = managed_router(tmp_path)
    rows = [{"alias": name, "server_name": "custom:" + name, "status": "custom",
             "client_config": {"transport": "http", "url": "https://" + host + ".test/mcp"}}
            for name, host in [("docs", "short"), (long_alias, "long")]]

    def profile(request):
        alias = request.url.params.get("alias")
        return httpx.Response(200, json={"organization_admission": {"required": False},
                                        "servers": [r for r in rows if alias is None or r["alias"] == alias]})

    with respx.mock(assert_all_called=False) as api:
        api.get(REGISTRY + "/integrations/profiles/default/servers").mock(side_effect=profile)
        await router._call_target("default", "docs__read")
        assert router._profile_aliases[("default", "docs")] == "docs"
        routed, selected = await router._call_target("default", wire)
        assert routed.upstream_name == long_alias
        assert routed.original_name == "read"
        assert str(selected.config.url) == "https://long.test/mcp"


@pytest.mark.asyncio
async def test_sender_does_not_retain_request_context():
    from lightnow_proxy.request_context import current_mcp_client_context

    delivered = []

    class Registry:
        async def post_runtime_event(self, event):
            assert current_mcp_client_context.get() is None
            delivered.append(event)

    router = telemetry_router(Registry())
    token = current_mcp_client_context.set({"name": "first-client", "version": "1"})
    try:
        await router._emit_runtime_event({"event_type": "first"})
    finally:
        current_mcp_client_context.reset(token)
    await router.close_runtime_events()
    assert delivered[0]["mcp_client_name"] == "first-client"
    assert delivered[0]["mcp_client_version"] == "1"


@pytest.mark.asyncio
async def test_startup_failure_closes_sessions_and_queued_telemetry(monkeypatch):
    from contextlib import asynccontextmanager
    from lightnow_proxy.app import create_app

    opened, closed = [], []

    class Registry:
        async def post_runtime_event(self, event):
            await asyncio.Event().wait()

    router = telemetry_router(Registry())
    router.config.profiles = {"first": ProfileConfig(), "second": ProfileConfig()}

    @asynccontextmanager
    async def manager_run(_self):
        index = len(opened)
        opened.append(index)
        if index == 1:
            raise RuntimeError("SESSION_START_FAILED")
        try:
            yield
        finally:
            closed.append(index)

    monkeypatch.setattr("lightnow_proxy.app.StreamableHTTPSessionManager.run", manager_run)
    monkeypatch.setattr("lightnow_proxy.router.TELEMETRY_DRAIN_SECONDS", .01)
    app = create_app(router.config, router=router)
    await router._emit_runtime_event({"event_type": "queued-before-startup"})
    with pytest.raises(RuntimeError, match="SESSION_START_FAILED"):
        async with app.router.lifespan_context(app):
            raise AssertionError("startup must not succeed")
    assert opened == [0, 1] and closed == [0]
    assert router.runtime_telemetry_status()["pending"] == 0
    assert router.runtime_telemetry_status()["dropped"] == 1
    assert router._telemetry_closed


@pytest.mark.asyncio
async def test_cancelled_http_lifecycle_drains_final_event():
    from lightnow_proxy.app import create_app

    events = []
    ready = asyncio.Event()

    class Registry:
        async def post_runtime_event(self, event):
            events.append(event)

    router = telemetry_router(Registry())
    router.config.local_proxy.enabled = True
    app = create_app(router.config, router=router)

    async def serving():
        async with app.router.lifespan_context(app):
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(serving())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e["event_type"] for e in events] == ["runner_started", "runner_stopped"]
    assert router.runtime_telemetry_status()["pending"] == 0
    assert router._telemetry_closed


@pytest.mark.asyncio
async def test_real_mcp_response_precedes_optional_delivery():
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from lightnow_proxy.app import create_app
    from test_mcp_app import run_math_upstream_server

    release = asyncio.Event()
    delivered = []

    class Registry:
        async def post_runtime_event(self, event):
            await release.wait()
            delivered.append(event)

    async with run_math_upstream_server() as upstream:
        router = telemetry_router(Registry())
        router.config.local_proxy.enabled = True
        router.config.local_proxy.profile = "default"
        router.config.profiles["default"].upstreams = ["math"]
        router.config.upstreams["math"] = UpstreamConfig(url=upstream)
        app = create_app(router.config, router=router)
        try:
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8080") as http:
                    async with Client(streamable_http_client("http://127.0.0.1:8080/mcp", http_client=http)) as client:
                        await client.list_tools()
                        result = await asyncio.wait_for(client.call_tool("math__add", {"left": 2, "right": 3}), 10)
                        assert result.is_error is False
                        assert result.content[0].text == "5"
                        assert delivered == []
                        release.set()
        finally:
            release.set()
            await router.close_runtime_events()
    calls = [e for e in delivered if e["event_type"] == "call_tool"]
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "math__add"
    assert calls[0]["status"] == "success"
