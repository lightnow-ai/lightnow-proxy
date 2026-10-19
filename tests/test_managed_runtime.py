from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from datetime import UTC, datetime, timedelta
import json
import re
from pathlib import Path
import sys
from time import monotonic, time
from uuid import UUID, uuid4

import httpx
import httpx2
import jwt
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
import pytest
import respx

from lightnow_proxy.app import create_app
from lightnow_proxy.config import AuthConfig, ProfileConfig, ProxyConfig, RegistryApiConfig, RuntimeUpstreamConfig, UpstreamConfig
from lightnow_proxy.registry import ManagedRuntime, RegistryApiError
from lightnow_proxy.router import ToolRouter, unwrap_error
from lightnow_proxy.runtime_secrets import RuntimeSecretResolver
from lightnow_proxy.config import RuntimeSecretsConfig
from lightnow_proxy.upstream import ToolDiscoveryError, ToolInventory
from test_mcp_app import run_math_upstream_server

REGISTRY = "https://registry.example.test/v0.1"
CLIENT = str(uuid4())
CONNECTION = str(uuid4())
ASSIGNMENT = str(uuid4())
DISCOVERY = str(uuid4())
TOOL_PREFIX = f"ln_{UUID(ASSIGNMENT).hex}"


def managed_context() -> dict:
    now = int(time())
    actor, owner = str(uuid4()), str(uuid4())
    return {
        "schema_version": 1, "purpose": "managed-discovery", "actor_id": actor, "owner_type": "tenant", "owner_id": owner,
        "runtime_client_id": CLIENT, "profile": "default", "profile_id": str(uuid4()), "profile_revision": 2,
        "assignment_id": ASSIGNMENT, "assignment_revision": 2, "connection_id": CONNECTION, "connection_revision": 1,
        "credential_principal_type": "user", "credential_principal_id": actor, "credential_binding_id": None, "credential_revision": 0,
        "policy_revision": 1, "admission_required": True, "source_sequence": 1, "source_expires_at": now + 45,
        "issued_at": now, "expires_at": now + 45, "context_proof": "private-proof-do-not-log", "reporting_proof": "private-report-do-not-log",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,target", [("http", "header"), ("stdio", "env")])
async def test_managed_context_reuses_external_secret_resolution_and_original_deadline(tmp_path, transport, target, monkeypatch):
    router = managed_router(tmp_path)
    client = router.registry_client
    client.runtime_client_id = CLIENT
    config = {"transport": transport, **({"url": "https://fixture.example.test/mcp"} if transport == "http" else {"command": "fixture"})}
    item = {"alias": "docs", "connection_id": CONNECTION, "assignment_id": ASSIGNMENT, "status": "linked",
            "managed": managed_context(), "client_config": config, "context": {
                "probe_request": {"transport": transport, transport: {}}, "external_secret_bindings": [{
                    "provider": {"id": "fixture-provider", "provider_type": "vault_kv_v2", "resolution_mode": "runtime", "config": {}},
                    "locator": {"path": "secret/data/fixture", "field": "token"}, "target": {"type": target, "name": "FIXTURE_TOKEN"},
                }],
            }}
    client.runtime_secret_resolver = None
    with pytest.raises(RegistryApiError, match="RUNTIME_SECRET_RESOLVER_REQUIRED"):
        await client._profile_server_to_upstream(item, "default", None)
    client.runtime_secret_resolver = RuntimeSecretResolver(RuntimeSecretsConfig())
    with respx.mock(assert_all_called=True) as mock:
        vault = mock.get("http://127.0.0.1:8200/v1/secret/data/fixture").respond(200, json={"data": {"data": {"token": "fixture-secret"}}})
        resolved = await client._profile_server_to_upstream(item, "default", None)
        assert (resolved.config.headers if transport == "http" else resolved.config.env)["FIXTURE_TOKEN"] == "fixture-secret"
        assert vault.call_count == 1
        # External IO cannot extend the authority that preceded it.
        with pytest.raises(RegistryApiError, match="MANAGED_AUTHORITY_EXPIRED"):
            await client._profile_server_to_upstream(item, "default", None, started=monotonic() - 60)
        assert vault.call_count == 1
        native_resolve = client.runtime_secret_resolver.resolve_context
        clock = monotonic()

        async def resolve_after_expiry(context):
            result = await native_resolve(context)
            monkeypatch.setattr("lightnow_proxy.registry.monotonic", lambda: clock + 60)
            return result

        monkeypatch.setattr(client.runtime_secret_resolver, "resolve_context", resolve_after_expiry)
        with pytest.raises(RegistryApiError, match="MANAGED_AUTHORITY_EXPIRED"):
            await client._profile_server_to_upstream(item, "default", None, started=clock)
        assert vault.call_count == 2


@asynccontextmanager
async def managed_upstream(transport: str, tmp_path: Path, *, read_name: str = "read"):
    if transport == "stdio":
        trace = tmp_path / "dispatches.txt"
        server = tmp_path / "server.py"
        server.write_text('''from pathlib import Path
from mcp.server.mcpserver import MCPServer
mcp = MCPServer("managed")
@mcp.tool()
def read(value: str) -> str:
    with Path(TRACE).open("a") as output:
        output.write("read\\n")
    return value
@mcp.tool()
def write(value: str) -> str:
    with Path(TRACE).open("a") as output:
        output.write("write\\n")
    return value
mcp.run(transport="stdio")
'''.replace("TRACE", repr(str(trace))))
        yield {"transport": "stdio", "command": sys.executable, "args": [str(server)]}, lambda: trace.read_text().splitlines() if trace.exists() else []
        return
    calls = []

    async def list_tools(_context, params):
        return ListToolsResult(tools=[Tool(name=read_name if params.cursor is None else "write", input_schema={
            "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"],
        })], next_cursor="page-2" if params.cursor is None else None, ttl_ms=0, cache_scope="private")

    async def call_tool(_context, params):
        calls.append(params.name)
        return CallToolResult(content=[TextContent(type="text", text=params.arguments["value"])])

    async with run_math_upstream_server(Server("managed", version="1.0.0", on_list_tools=list_tools, on_call_tool=call_tool)) as url:
        yield {"transport": "http", "url": url}, lambda: calls


def managed_router(tmp_path: Path) -> ToolRouter:
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"access_token": jwt.encode({"sub": "actor", "iss": "https://auth.example.test", "exp": int(time()) + 3600}, key="", algorithm="none"),
                                   "issuer": "https://auth.example.test", "client_id": "lightnow-cli", "context_type": "personal"}))
    return ToolRouter(ProxyConfig(
        auth=AuthConfig(enabled=False, issuer="https://auth.example.test"),
        local_proxy={"enabled": True, "sync_from_lightnow": True, "telemetry_enabled": False, "tool_cache_path": str(tmp_path / "tools.json")},
        registry_api=RegistryApiConfig(enabled=True, base_url=REGISTRY, use_cli_session=True, cli_config_path=str(session), include_secrets=True),
        profiles={"default": ProfileConfig(upstreams=["docs", "bypass"], runtime_upstreams=[RuntimeUpstreamConfig(
            name="legacy-bypass", server="public.example/bypass", version="1.0.0")])},
        upstreams={"docs": UpstreamConfig(url="https://must-not-run.example.test/mcp"), "bypass": UpstreamConfig(url="https://must-not-run.example.test/mcp")},
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_heartbeat_executes_exact_queued_inventory_without_business_call(tmp_path, transport):
    async with managed_upstream(transport, tmp_path) as (config, calls):
        router = managed_router(tmp_path)
        router.config.local_proxy.device_installation_id = str(uuid4())
        router.config.local_proxy.client_instance_id = str(uuid4())
        router.config.local_proxy.device_hostname = "fixture"
        context = managed_context()
        request = {key: value for key, value in context.items() if key in {
            "connection_id", "connection_revision", "profile_id", "profile_revision", "policy_revision", "profile",
            "assignment_id", "assignment_revision", "runtime_client_id", "credential_binding_id", "credential_revision",
        }}
        request.update(id=DISCOVERY, status="queued", etag='"fixture:1"', expires_at=(datetime.now(UTC) + timedelta(seconds=120)).isoformat())
        item = {"alias": "docs", "connection_id": CONNECTION, "assignment_id": ASSIGNMENT, "status": "managed_runtime_required"}
        reports = []

        def profile_response(incoming):
            entry = dict(item)
            if incoming.url.params.get("include") == "secrets":
                assert incoming.url.params["alias"] == "docs"
                entry.update(status="linked", managed=context, client_config=config)
            return httpx.Response(200, json={"servers": [entry]})

        def report_response(incoming):
            assert incoming.headers["If-Match"] == request["etag"]
            body = json.loads(incoming.content)
            reports.append(body)
            assert body["context_proof"] == context["context_proof"]
            assert body["status"] == "complete" and body["page_count"] == (2 if transport == "http" else 1)
            assert {tool["name"] for tool in body["tools"]} == {"read", "write"}
            return httpx.Response(200, json={"authorization": {"schema_version": 1, "expires_at": context["expires_at"], "allowed_tools": []}})

        with respx.mock(assert_all_called=True) as mock:
            mock.post(re.compile(REGISTRY + r"/integrations/devices/.*/clients/.*/heartbeat")).respond(200, json={
                "device": {"clients": [{"id": CLIENT, "client_instance_id": str(router.config.local_proxy.client_instance_id)}]},
                "managed_discovery_requests": [request, request],
            })
            mock.get(REGISTRY + "/integrations/profiles/default/servers").mock(side_effect=profile_response)
            mock.put(REGISTRY + f"/integrations/connections/{CONNECTION}/tool-discoveries/{DISCOVERY}").mock(side_effect=report_response)
            assert await router.send_device_heartbeat()
            assert len(router._discovery_tasks) == 1
            await asyncio.gather(*router._discovery_tasks.values())
        assert len(reports) == 1 and calls() == []
        assert router.device_heartbeat_status()["interval_seconds"] == 15


@pytest.mark.asyncio
async def test_discovery_scheduler_bounds_work_and_shutdown_cancels_it(tmp_path, monkeypatch):
    router = managed_router(tmp_path)
    router.registry_client.runtime_client_id = CLIENT
    stopped = []

    async def wait_for_shutdown(request):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(request["id"])

    monkeypatch.setattr(router, "_run_discovery", wait_for_shutdown)
    request = {"profile": "default", "runtime_client_id": CLIENT, "status": "queued",
               "expires_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat()}
    router._schedule_discoveries([{**request, "id": str(uuid4())} for _ in range(20)])
    await asyncio.sleep(0)
    assert len(router._discovery_tasks) == 2

    async def heartbeat():
        raise asyncio.CancelledError

    monkeypatch.setattr(router, "send_device_heartbeat", heartbeat)
    with pytest.raises(asyncio.CancelledError):
        await router.run_device_heartbeat_loop()
    assert len(stopped) == 2 and not router._discovery_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["profile_revision", "policy_revision", "credential_revision", "connection_revision", "assignment_revision"])
async def test_queued_discovery_never_opens_upstream_after_input_revision_changed(tmp_path, monkeypatch, field):
    router = managed_router(tmp_path)
    router.registry_client.runtime_client_id = CLIENT
    context = managed_context()
    request = {**context, "id": DISCOVERY, "status": "queued", "etag": '"fixture:1"',
               "expires_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat()}
    request[field] -= 1
    opened = []

    @asynccontextmanager
    async def forbidden_session(_config):
        opened.append(True)
        raise AssertionError("Stale queued input reached upstream")
        yield

    monkeypatch.setattr(router.upstream_client, "session", forbidden_session)
    item = {"alias": "docs", "connection_id": CONNECTION, "assignment_id": ASSIGNMENT, "status": "linked",
            "managed": context, "client_config": {"transport": "http", "url": "https://fixture.example.test/mcp"}}
    with respx.mock(assert_all_called=True) as mock:
        mock.get(REGISTRY + "/integrations/profiles/default/servers").respond(200, json={"servers": [item]})
        await router._run_discovery(request)
    assert not opened


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status,reason", [
    (ToolDiscoveryError("DISCOVERY_LIMIT_EXCEEDED"), "partial", "DISCOVERY_LIMIT_EXCEEDED"),
    (RuntimeError("private upstream credentials and definitions"), "startup_failed", "RUNTIME_START_FAILED"),
])
async def test_queued_discovery_reports_failure_without_private_error_or_partial_manifest(tmp_path, monkeypatch, failure, status, reason):
    router = managed_router(tmp_path)
    router.registry_client.runtime_client_id = CLIENT
    context = managed_context()
    request = {**context, "id": DISCOVERY, "status": "queued", "etag": '"fixture:1"',
               "expires_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat()}

    @asynccontextmanager
    async def failed_session(_config):
        raise failure
        yield

    monkeypatch.setattr(router.upstream_client, "session", failed_session)
    item = {"alias": "docs", "connection_id": CONNECTION, "assignment_id": ASSIGNMENT, "status": "linked",
            "managed": context, "client_config": {"transport": "http", "url": "https://fixture.example.test/mcp"}}
    with respx.mock(assert_all_called=True) as mock:
        mock.get(REGISTRY + "/integrations/profiles/default/servers").respond(200, json={"servers": [item]})
        report = mock.put(REGISTRY + f"/integrations/connections/{CONNECTION}/tool-discoveries/{DISCOVERY}").respond(200, json={"data": {"status": status}})
        await router._run_discovery(request)
        body = json.loads(report.calls.last.request.content)
        assert body == {"context_proof": context["context_proof"], "status": status, "reason_code": reason,
                        "page_count": 0, "duration_ms": body["duration_ms"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_managed_profile_filters_actual_inventory_and_denies_before_single_dispatch(tmp_path, transport, monkeypatch):
    async with managed_upstream(transport, tmp_path) as (client_config, calls):
        router = managed_router(tmp_path)
        router.registry_client.runtime_client_id = CLIENT
        contexts = []
        state = {"allow": True, "available": True}
        reports = []
        facts = []
        expired = False
        # This obsolete cache must never revive a hidden tool.
        (tmp_path / "tools.json").write_text(json.dumps({"generated_at": time(), "tools": [Tool(name=f"{TOOL_PREFIX}__write", input_schema={}).model_dump(by_alias=True)]}))

        def profile_response(request):
            if not state["available"]:
                return httpx.Response(403)
            item = {"alias": "docs", "connection_id": CONNECTION, "assignment_id": ASSIGNMENT, "status": "managed_runtime_required"}
            if request.url.params.get("include") == "secrets":
                assert request.headers["X-LightNow-Runtime-Capabilities"] == "managed-connections.v1"
                assert request.headers["X-LightNow-Runtime-Client"] == CLIENT
                context = managed_context()
                contexts.append(context)
                item.update(status="linked", managed=context, client_config=client_config)
            return httpx.Response(200, json={"servers": [item], "organization_admission": {"required": True, "policy_revision": 1}})

        def report_response(request):
            report = json.loads(request.content)
            reports.append(report)
            assert report["context_proof"] == contexts[-1]["context_proof"]
            assert {tool["name"] for tool in report["tools"]} == {"read", "write"}
            assert report["page_count"] == (2 if transport == "http" else 1)
            allowed = ["read"] if state["allow"] else []
            authorization = {"schema_version": 1, "expires_at": contexts[-1]["expires_at"], "allowed_tools": allowed}
            if "original_tool_name" in report:
                name = report["original_tool_name"]
                authorization["requested_tool"] = {"name": name, "allowed": name in allowed, "reason_code": None if name in allowed else "TOOL_NOT_SELECTED"}
            if expired:
                import lightnow_proxy.registry as registry
                monkeypatch.setattr(registry, "monotonic", lambda: monotonic() + 70)
            return httpx.Response(200, json={"authorization": authorization})

        with respx.mock(assert_all_called=False) as api:
            def accept_event(request):
                facts.append(json.loads(request.content))
                assert "X-Tenant" not in request.headers
                return httpx.Response(201, json={"event": json.loads(request.content)})

            api.post(REGISTRY + "/integrations/runtime-events").mock(side_effect=accept_event)
            api.get(REGISTRY + "/integrations/profiles/default/servers").mock(side_effect=profile_response)
            base = REGISTRY + f"/integrations/connections/{CONNECTION}/tool-discoveries"
            api.post(base).respond(201, json={"data": {"id": DISCOVERY}}, headers={"ETag": '"attempt:1"'})
            api.put(base + "/" + DISCOVERY).mock(side_effect=report_response)
            assert [tool.name for tool in await router.list_tools("default")] == [f"{TOOL_PREFIX}__read"]
            with pytest.raises(Exception) as denied:
                await router.call_tool("default", f"{TOOL_PREFIX}__write", {"value": "sensitive-value"})
            assert str(unwrap_error(denied.value)) == "MANAGED_TOOL_DENIED"
            assert calls() == []
            # Exercise the actual downstream MCP transport as well as the router.
            app = create_app(router.config, router=router)
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8080") as http:
                    async with Client(streamable_http_client("http://127.0.0.1:8080/mcp", http_client=http)) as downstream:
                        denied_result = await downstream.session.call_tool(f"{TOOL_PREFIX}__write", arguments={"value": "sensitive-value"})
                        assert denied_result.is_error is True
                        assert "MANAGED_TOOL_DENIED" in denied_result.content[0].text
            assert calls() == []
            result = await router.call_tool("default", f"{TOOL_PREFIX}__read", {"value": "ok"})
            assert result.is_error is False
            assert calls() == ["read"]
            # Failing the durable pre-handoff commit must leave the actual SDK
            # upstream untouched, while retaining the attempted/runtime failure.
            from lightnow_proxy.managed_events import EvidenceStoreError
            native_record = router._event_outbox().record
            def fail_handoff_once(call, phase, reason=None):
                if phase == "dispatch_started":
                    raise EvidenceStoreError()
                return native_record(call, phase, reason)
            with monkeypatch.context() as storage_fault:
                storage_fault.setattr(router._event_outbox(), "record", fail_handoff_once)
                with pytest.raises(Exception) as unavailable:
                    await router.call_tool("default", f"{TOOL_PREFIX}__read", {"value": "sensitive-value"})
                assert str(unwrap_error(unavailable.value)) == "EVIDENCE_STORE_UNAVAILABLE"
            assert calls() == ["read"]
            expired = True
            with monkeypatch.context() as clocks:
                # Scope the changed clock to this response/dispatch attempt.
                monkeypatch = clocks
                with pytest.raises(Exception) as expiry:
                    await router.call_tool("default", f"{TOOL_PREFIX}__read", {"value": "sensitive-value"})
                assert str(unwrap_error(expiry.value)) == "MANAGED_AUTHORITY_EXPIRED"
            expired = False
            assert calls() == ["read"]
            state["allow"] = False
            with pytest.raises(Exception):
                await router.call_tool("default", f"{TOOL_PREFIX}__read", {"value": "sensitive-value"})
            assert calls() == ["read"]
            for bypass in ["bypass__read", "legacy-bypass__read"]:
                with pytest.raises(Exception):
                    await router.call_tool("default", bypass, {})
            state["available"] = False
            with pytest.raises(httpx.HTTPStatusError):
                await router.list_tools("default")
            assert calls() == ["read"]
            await router.flush_managed_events()
            assert router.managed_event_status()["pending"] == 0
            assert {fact["phase"] for fact in facts} >= {"attempted", "denied", "dispatch_started", "dispatched", "succeeded"}
            assert any(fact.get("reason_code") == "TOOL_NOT_SELECTED" for fact in facts)
            assert any(fact["phase"] == "runtime_failed" and fact.get("reason_code") == "EVIDENCE_STORE_UNAVAILABLE" for fact in facts)
            assert sum(fact["phase"] == "succeeded" for fact in facts) == 1
            assert sum(fact["phase"] == "dispatch_started" for fact in facts) == 1
            assert all(fact["schema_version"] == 2 for fact in facts)
            assert all("observation_id" in fact for fact in facts if fact["phase"] == "succeeded")
            assert "sensitive-value" not in json.dumps(facts)
            assert not any(key in fact for fact in facts for key in {"arguments", "result", "request_bytes", "error_message", "server_alias"})
        assert "sensitive-value" not in json.dumps(reports)
        assert "private-proof-do-not-log" not in repr(await router.registry_client._profile_server_to_upstream(
            {"alias": "docs", "connection_id": CONNECTION, "assignment_id": ASSIGNMENT, "status": "linked", "managed": managed_context(), "client_config": client_config},
            "default", None))


def test_managed_authority_cannot_extend_on_wall_clock_rollback_or_exceed_source(monkeypatch):
    import lightnow_proxy.registry as registry
    value = managed_context()
    lease = ManagedRuntime.model_validate({**value, "deadline": monotonic() - 1})
    monkeypatch.setattr(registry, "time", lambda: value["issued_at"] - 1000)
    with pytest.raises(RegistryApiError, match="MANAGED_AUTHORITY_EXPIRED"):
        lease.assert_current()
    beyond_source = ManagedRuntime.model_validate({**value, "source_expires_at": value["expires_at"] - 1, "deadline": monotonic() + 60})
    with pytest.raises(RegistryApiError, match="MANAGED_AUTHORITY_EXPIRED"):
        beyond_source.assert_current()


@pytest.mark.asyncio
async def test_native_heartbeat_negotiates_owned_client_without_optional_telemetry(tmp_path):
    router = managed_router(tmp_path)
    local = router.config.local_proxy
    local.device_installation_id = uuid4()
    local.client_instance_id = uuid4()
    local.device_hostname = "managed-test"
    local.device_platform = "linux"
    assert router.device_heartbeat_enabled()
    with respx.mock(assert_all_called=True) as api:
        response = api.post(REGISTRY + f"/integrations/devices/{local.device_installation_id}/clients/{local.client_instance_id}/heartbeat").respond(
            200, json={"device": {"clients": [{"id": CLIENT, "client_instance_id": str(local.client_instance_id)}]}})
        assert await router.send_device_heartbeat()
        assert json.loads(response.calls.last.request.content)["client"]["managed_capabilities"] == ["managed-connections.v1"]
        assert response.calls.last.request.headers["Authorization"].startswith("Bearer ")
        assert router.registry_client.runtime_client_id == CLIENT


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unknown_name", "renewed_expiry", "wrong_requested_name"])
async def test_runtime_rejects_invalid_authorization_response_without_broadening_lease(tmp_path, fault):
    router = managed_router(tmp_path)
    context = managed_context()
    lease = ManagedRuntime.model_validate({**context, "deadline": monotonic() + 45})
    authorization = {"schema_version": 1, "expires_at": lease.expires_at, "allowed_tools": ["read"],
                     "requested_tool": {"name": "read", "allowed": True, "reason_code": None}}
    if fault == "unknown_name":
        authorization["allowed_tools"] = ["not-observed"]
    elif fault == "renewed_expiry":
        authorization["expires_at"] += 1
    else:
        authorization["requested_tool"]["name"] = "other"
    with respx.mock(assert_all_called=True) as api:
        base = REGISTRY + f"/integrations/connections/{CONNECTION}/tool-discoveries"
        api.post(base).respond(201, json={"data": {"id": DISCOVERY}}, headers={"ETag": '"attempt:1"'})
        api.put(base + "/" + DISCOVERY).respond(200, json={"authorization": authorization})
        with pytest.raises(RegistryApiError, match="INVALID_TOOL_AUTHORIZATION"):
            await router.registry_client.authorize_inventory(lease, ToolInventory([Tool(name="read", input_schema={})], 1, 1), "read")


@pytest.mark.asyncio
async def test_managed_names_stay_bound_to_assignment_through_alias_collision_rename_and_replacement(tmp_path):
    other = str(uuid4())
    other_connection = str(uuid4())
    async with managed_upstream("http", tmp_path, read_name="_read") as (first_config, first_calls):
        async with managed_upstream("http", tmp_path) as (second_config, second_calls):
            router = managed_router(tmp_path)
            router.registry_client.runtime_client_id = CLIENT
            # Both had the same old alias-based wire name: docs___read.
            records = {
                ASSIGNMENT: {"alias": "docs", "connection_id": CONNECTION, "config": first_config, "tool": "_read"},
                other: {"alias": "docs_", "connection_id": other_connection, "config": second_config, "tool": "read"},
            }
            allowed = {ASSIGNMENT}
            contexts = {}
            required = True
            pending_replacement = None

            def profile_response(request):
                nonlocal pending_replacement
                if request.url.params.get("include") == "secrets" and pending_replacement is not None:
                    records[pending_replacement] = records.pop(ASSIGNMENT)
                    allowed.remove(ASSIGNMENT)
                    allowed.add(pending_replacement)
                    pending_replacement = None
                items = []
                for assignment, record in records.items():
                    if request.url.params.get("alias") not in (None, record["alias"]):
                        continue
                    item = {"alias": record["alias"], "connection_id": record["connection_id"], "assignment_id": assignment,
                            "status": "managed_runtime_required"}
                    if request.url.params.get("include") == "secrets":
                        context = {**managed_context(), "assignment_id": assignment, "connection_id": record["connection_id"], "admission_required": required}
                        contexts[assignment] = context
                        item.update(status="linked", managed=context, client_config=record["config"])
                    items.append(item)
                return httpx.Response(200, json={"servers": items, "organization_admission": {"required": required, "policy_revision": 1}})

            def report_response(request, connection):
                assignment, record = next((key, value) for key, value in records.items() if value["connection_id"] == connection)
                body = json.loads(request.content)
                names = [record["tool"]] if assignment in allowed else []
                authorization = {"schema_version": 1, "expires_at": contexts[assignment]["expires_at"], "allowed_tools": names}
                if "original_tool_name" in body:
                    authorization["requested_tool"] = {"name": body["original_tool_name"], "allowed": body["original_tool_name"] in names, "reason_code": None if body["original_tool_name"] in names else "TOOL_NOT_SELECTED"}
                return httpx.Response(200, json={"authorization": authorization})

            with respx.mock(assert_all_called=False) as api:
                api.get(REGISTRY + "/integrations/profiles/default/servers").mock(side_effect=profile_response)
                for connection in [CONNECTION, other_connection]:
                    base = REGISTRY + f"/integrations/connections/{connection}/tool-discoveries"
                    api.post(base).mock(side_effect=lambda request: httpx.Response(201, json={"data": {"id": str(uuid4())}}, headers={"ETag": '"1"'}))
                    api.put(url__regex=re.escape(base) + r"/[0-9a-f-]+$").mock(side_effect=lambda request, identity=connection: report_response(request, identity))
                first_wire = f"{TOOL_PREFIX}___read"
                second_wire = f"ln_{UUID(other).hex}__read"
                tools = await router.list_tools("default")
                assert [tool.name for tool in tools] == [first_wire]
                assert tools[0].description.startswith("[docs]")
                assert tools[0].meta["lightnow_proxy_assignment_id"] == ASSIGNMENT
                assert first_calls() == second_calls() == []
                with pytest.raises(Exception):
                    await router.call_tool("default", second_wire, {"value": "denied"})
                assert second_calls() == []
                assert not (await router.call_tool("default", first_wire, {"value": "ok"})).is_error
                assert first_calls() == ["_read"]
                # Names stay stable when aliases are exchanged.
                records[ASSIGNMENT]["alias"], records[other]["alias"] = "docs_", "docs"
                assert [tool.name for tool in await router.list_tools("default")] == [first_wire]
                assert not (await router.call_tool("default", first_wire, {"value": "renamed"})).is_error
                assert first_calls() == ["_read", "_read"]
                assert second_calls() == []
                # A replacement between metadata and context delivery is also rejected.
                pending_replacement = str(uuid4())
                with pytest.raises(Exception) as replaced:
                    await router.call_tool("default", first_wire, {"value": "rebound"})
                assert str(unwrap_error(replaced.value)) == "MANAGED_CONTEXT_MISMATCH"
                required = False
                # Even an exact local override cannot inherit the detached name.
                router.config.upstreams[TOOL_PREFIX] = UpstreamConfig(url=second_config["url"])
                router.config.profiles["default"].upstreams.append(TOOL_PREFIX)
                with pytest.raises(Exception):
                    await router.call_tool("default", first_wire, {"value": "stale"})
                assert first_calls() == ["_read", "_read"]
                assert second_calls() == []
