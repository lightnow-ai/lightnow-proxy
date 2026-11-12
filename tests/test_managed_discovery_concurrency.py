from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
import sys
from time import monotonic
from uuid import uuid4

import httpx
from mcp.types import Tool
import pytest
import respx

from lightnow_proxy.registry import ManagedRuntime, RegistryApiError
from lightnow_proxy.upstream import ToolInventory
from test_managed_runtime import CLIENT, CONNECTION, DISCOVERY, REGISTRY, managed_context, managed_router


def lease():
    return ManagedRuntime.model_validate({**managed_context(), "deadline": monotonic() + 45})


@pytest.mark.asyncio
async def test_inline_completion_makes_the_waiting_background_job_skip_upstream(tmp_path, monkeypatch):
    router = managed_router(tmp_path)
    client = router.registry_client
    client.runtime_client_id = CLIENT
    managed = lease()
    request = managed.model_dump(mode="json") | {"id": DISCOVERY, "status": "queued", "etag": '"old:1"',
        "expires_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat()}
    started, finish = asyncio.Event(), asyncio.Event()
    base = REGISTRY + f"/integrations/connections/{CONNECTION}/tool-discoveries"
    status = "queued"

    async def complete(_incoming):
        nonlocal status
        started.set()
        await finish.wait()
        status = "complete"
        return httpx.Response(200, json={"authorization": {"schema_version": 1,
            "expires_at": managed.expires_at, "allowed_tools": ["read"]}})

    @asynccontextmanager
    async def forbidden(_config):
        pytest.fail("A background job repeated completed inline metadata work")
        yield

    monkeypatch.setattr(router.upstream_client, "session", forbidden)
    item = {"alias": "docs", "connection_id": CONNECTION, "assignment_id": request["assignment_id"], "status": "linked",
            "managed": managed.model_dump(mode="json"), "client_config": {"transport": "http", "url": "https://fixture.example.test/mcp"}}
    with respx.mock(assert_all_called=True) as api:
        api.post(base).respond(201, json={"data": {"id": DISCOVERY}}, headers={"ETag": '"old:1"'})
        api.put(base + "/" + DISCOVERY).mock(side_effect=complete)
        profile = api.get(REGISTRY + "/integrations/profiles/default/servers").respond(200, json={"servers": [item]})
        current = api.get(base + "/" + DISCOVERY).mock(side_effect=lambda _incoming: httpx.Response(
            200, json={"data": {**request, "status": status}}, headers={"ETag": '"current:2"'}))
        inline = asyncio.create_task(client.authorize_inventory(managed, ToolInventory([Tool(name="read", input_schema={})], 1, 1)))
        background = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            background = asyncio.create_task(router._run_discovery(request))
            async with asyncio.timeout(2):
                while profile.call_count < 2:
                    await asyncio.sleep(0.01)
            assert current.call_count == 0
            finish.set()
            assert await asyncio.wait_for(inline, 2) == {"read"}
            await asyncio.wait_for(background, 2)
            assert current.call_count == 1
        finally:
            finish.set()
            await asyncio.gather(inline, *([background] if background else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_parallel_authorizations_have_separate_observations_and_fresh_decisions(tmp_path):
    clients = [managed_router(tmp_path).registry_client for _ in range(2)]
    managed = lease()
    inventory = ToolInventory([Tool(name="read", input_schema={})], 1, 1)
    base = REGISTRY + f"/integrations/connections/{CONNECTION}/tool-discoveries"
    pending = None
    created, reports = [], []

    def start(_request):
        nonlocal pending
        if pending is None:
            pending = str(uuid4())
            created.append(pending)
        return httpx.Response(201, json={"data": {"id": pending}}, headers={"ETag": f'"{pending}:1"'})

    async def finish(request):
        nonlocal pending
        attempt = pending
        await asyncio.sleep(0.02)
        if attempt is None or pending != attempt:
            return httpx.Response(412)
        assert request.headers["If-Match"] == f'"{attempt}:1"'
        reports.append(attempt)
        pending = None
        return httpx.Response(200, json={"authorization": {"schema_version": 1,
            "expires_at": managed.expires_at, "allowed_tools": ["read"]}})

    with respx.mock(assert_all_called=True) as api:
        api.post(base).mock(side_effect=start)
        api.put(url__startswith=base + "/").mock(side_effect=finish)
        result = await asyncio.gather(*(client.authorize_inventory(managed, inventory) for client in clients))
    assert result == [{"read"}, {"read"}]
    assert len(set(created)) == 2 and reports == created


@pytest.mark.asyncio
@pytest.mark.parametrize("different", [False, True])
async def test_scope_coordinates_with_a_real_second_process_and_keeps_other_assignments_parallel(tmp_path, different):
    client = managed_router(tmp_path).registry_client
    managed = lease()
    code = '''import asyncio,json,sys
from lightnow_proxy.config import RegistryApiConfig
from lightnow_proxy.registry import RegistryApiClient,ManagedRuntime
async def main():
 data=json.loads(sys.stdin.readline())
 client=RegistryApiClient(RegistryApiConfig.model_validate(data['config']))
 async with client.discovery_scope(ManagedRuntime.model_validate(data['managed'])):
  print('locked',flush=True)
  sys.stdin.readline()
asyncio.run(main())
'''
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", code,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    waiting = None
    entered = asyncio.Event()
    try:
        child.stdin.write((json.dumps({"config": client.config.model_dump(mode="json"),
                                      "managed": managed.model_dump(mode="json") | {"deadline": managed.deadline}}) + "\n").encode())
        await child.stdin.drain()
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"locked\n"

        async def enter():
            other = managed.model_copy(update={"assignment_id": uuid4()}) if different else managed
            async with client.discovery_scope(other):
                entered.set()

        waiting = asyncio.create_task(enter())
        if different:
            await asyncio.wait_for(entered.wait(), 2)
        else:
            await asyncio.sleep(0.1)
            assert not entered.is_set()
        child.stdin.write(b"release\n")
        await child.stdin.drain()
        await asyncio.wait_for(waiting, 3)
        assert await asyncio.wait_for(child.wait(), 3) == 0
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()
        if waiting and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_wait_releases_eventual_acquisition_and_expired_authority_cannot_enter(tmp_path):
    client = managed_router(tmp_path).registry_client
    managed = lease()
    entered = asyncio.Event()

    async def wait():
        async with client.discovery_scope(managed):
            entered.set()

    async with client.discovery_scope(managed):
        waiting = asyncio.create_task(wait())
        await asyncio.sleep(0.1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    assert not entered.is_set()
    async with asyncio.timeout(2):
        async with client.discovery_scope(managed):
            pass
    expired = managed.model_copy(update={"deadline": monotonic() - 1})
    with pytest.raises(RegistryApiError, match="MANAGED_AUTHORITY_EXPIRED"):
        async with client.discovery_scope(expired):
            pytest.fail("Expired authority acquired a metadata scope")


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", [False, True])
async def test_waiting_never_reuses_old_permission_or_renews_authority(tmp_path, monkeypatch, expiry):
    client = managed_router(tmp_path).registry_client
    managed = lease()
    inventory = ToolInventory([Tool(name="read", input_schema={})], 1, 1)
    base = REGISTRY + f"/integrations/connections/{CONNECTION}/tool-discoveries"
    original_clock = monotonic()
    with respx.mock(assert_all_called=False) as api:
        denied = api.post(base).respond(403)
        async with client.discovery_scope(managed):
            waiting = asyncio.create_task(client.authorize_inventory(managed, inventory))
            await asyncio.sleep(0.1)
            assert denied.call_count == 0
            if expiry:
                monkeypatch.setattr("lightnow_proxy.registry.monotonic", lambda: original_clock + 60)
        if expiry:
            with pytest.raises(RegistryApiError, match="MANAGED_AUTHORITY_EXPIRED"):
                await waiting
            assert denied.call_count == 0
        else:
            with pytest.raises(httpx.HTTPStatusError) as failure:
                await waiting
            assert failure.value.response.status_code == 403
            assert denied.call_count == 1


@pytest.mark.asyncio
async def test_scope_timeout_is_bounded_and_does_not_issue_a_control_plane_request(tmp_path):
    client = managed_router(tmp_path).registry_client
    client.config.timeout_seconds = 0.05
    managed = lease()
    async with client.discovery_scope(managed):
        with pytest.raises(RegistryApiError, match="MANAGED_DISCOVERY_BUSY"):
            await client.authorize_inventory(managed, ToolInventory([], 1, 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["complete", "expired"])
async def test_queued_worker_rereads_terminal_state_without_opening_upstream(tmp_path, monkeypatch, status):
    router = managed_router(tmp_path)
    router.registry_client.runtime_client_id = CLIENT
    context = managed_context()
    request = {**context, "id": DISCOVERY, "status": "queued", "etag": '"old:1"',
               "expires_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat()}
    item = {"alias": "docs", "connection_id": CONNECTION, "assignment_id": context["assignment_id"], "status": "linked",
            "managed": context, "client_config": {"transport": "http", "url": "https://fixture.example.test/mcp"}}

    @asynccontextmanager
    async def forbidden(_config):
        pytest.fail("A terminal queued request opened the upstream")
        yield

    monkeypatch.setattr(router.upstream_client, "session", forbidden)
    with respx.mock(assert_all_called=True) as api:
        api.get(REGISTRY + "/integrations/profiles/default/servers").respond(200, json={"servers": [item]})
        api.get(REGISTRY + f"/integrations/connections/{CONNECTION}/tool-discoveries/{DISCOVERY}").respond(
            200, json={"data": {**request, "status": status}}, headers={"ETag": '"current:2"'})
        await router._run_discovery(request)
