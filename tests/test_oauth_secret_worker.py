from __future__ import annotations

import json
from uuid import uuid4

import httpx
import httpx2
import pytest
import respx

from test_oauth_secret_operations import oauth_claim, grant, kv, url, token_transport
from test_secret_worker import BASE, configuration, metadata, worker, receipt


@pytest.mark.asyncio
async def test_native_refresh_dispatch_is_durable_before_io_and_restart_only_reads_the_pair(tmp_path, monkeypatch):
    body = oauth_claim("oauth.refresh")
    config = configuration(tmp_path, body)
    runtime_id = str(uuid4())
    first = worker(config, runtime_id)
    calls, saved = [], {}
    def refresh(_request):
        with first.journal.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE terminal_at IS NULL").fetchone()
        assert row["dispatched"] == 1 and row["claim_id"] == body["claim"]["id"]
        return httpx2.Response(200, json={"access_token": "fresh-access", "refresh_token": "fresh-grant", "expires_in": 3600})
    tokens = token_transport(monkeypatch, refresh)
    def claimed(request):
        calls.append(json.loads(request.content)["request_id"])
        current = json.loads(json.dumps(body))
        if len(calls) > 1:
            current["claim"]["action"] = "oauth.readback"
        return httpx.Response(200, json={"data": current})
    def capture(role):
        def write(request):
            saved[role] = json.loads(request.content)["data"]["value"]
            return httpx.Response(200, json={"data": {"version": 2}})
        return write
    def reported(request):
        sent = json.loads(request.content)
        assert "oauth" in sent and "kv" not in sent and sent["runtime_client_id"] == runtime_id
        if len(calls) == 1:
            raise httpx.ReadError("result acknowledged remotely but response lost")
        return httpx.Response(200, json=receipt(body))
    with respx.mock as transport:
        transport.post(BASE+f"/integrations/secret-operations/{body['operation_id']}/claim").mock(side_effect=claimed)
        transport.get(url(body, "grant"), params={"version": 1}).respond(
            200, json=kv(json.dumps(grant(body)), body["grant_reference"]["operation_id"], 1))
        writes = []
        for role in ("access", "grant"):
            writes.append(transport.post(url(body, role)).mock(side_effect=capture(role)))
            transport.get(url(body, role), params={"version": 2}).mock(
                side_effect=lambda _request, role=role: httpx.Response(200, json=kv(saved[role], body["operation_id"])))
        transport.post(BASE+f"/integrations/secret-operations/{body['operation_id']}/result").mock(side_effect=reported)
        await first.personal_pending(metadata(body))
        restarted = worker(config, runtime_id)
        await restarted.personal_pending([])
    assert calls[0] == calls[1] and len(tokens) == 1
    assert all(route.call_count == 1 for route in writes)
    with restarted.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests WHERE terminal_at IS NULL").fetchone()[0] == 0
    journal = first.journal.path.read_bytes()
    assert b"fresh-access" not in journal and b"fresh-grant" not in journal


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["oauth.write", "oauth.refresh"])
async def test_repeated_write_or_refresh_instruction_cannot_dispatch_from_saved_journal(tmp_path, action):
    body = oauth_claim(action)
    config = configuration(tmp_path, body)
    instance = worker(config, str(uuid4()))
    from lightnow_proxy.secret_operations import OAuthOperation
    import hashlib
    from time import time
    identity, _ = await instance.registry.secret_worker_authorization(None, instance.registry.runtime_client_id)
    namespace = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    row = instance.journal.ensure(namespace, body["operation_id"], time()+600)
    instance.journal.stage(row, OAuthOperation.model_validate(body))
    with respx.mock as transport:
        transport.post(BASE+f"/integrations/secret-operations/{body['operation_id']}/claim").respond(200, json={"data": body})
        await instance.personal_pending([])
        assert len(transport.calls) == 1  # No Vault or token-provider side effect.
    assert instance.last_error == "SECRET_WORKER_STEP_UNCONFIRMED"


@pytest.mark.asyncio
async def test_lost_oauth_claim_response_keeps_nonce_and_never_refreshes_on_readback(tmp_path, monkeypatch):
    body = oauth_claim("oauth.refresh")
    instance = worker(configuration(tmp_path, body), str(uuid4()))
    tokens = token_transport(monkeypatch, lambda _: pytest.fail("Readback cannot refresh"))
    ids = []
    def claimed(request):
        ids.append(json.loads(request.content)["request_id"])
        if len(ids) == 1:
            raise httpx.ReadError("claim response lost")
        recovered = json.loads(json.dumps(body))
        recovered["claim"]["action"] = "oauth.readback"
        return httpx.Response(200, json={"data": recovered})
    with respx.mock as transport:
        transport.post(BASE+f"/integrations/secret-operations/{body['operation_id']}/claim").mock(side_effect=claimed)
        transport.get(url(body, "access"), params={"version": 2}).respond(404)
        result = transport.post(BASE+f"/integrations/secret-operations/{body['operation_id']}/result").respond(
            202, json=receipt(body, "outcome_unknown"))
        await instance.personal_pending(metadata(body))
        await instance.personal_pending([])
        assert all(call.request.method != "POST" or call.request.url.host == "registry.test" for call in transport.calls)
    assert ids[0] == ids[1] and len(ids) == 2 and not tokens
    assert json.loads(result.calls[0].request.content)["status"] == "outcome_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("org_connector", [False, True])
async def test_oauth_worker_negotiates_claim_and_result_before_dispatch(tmp_path, monkeypatch, org_connector):
    from lightnow_proxy.registry import RegistryApiClient
    from lightnow_proxy.secret_worker import SecretStoreWorker
    from test_secret_worker_org import configuration as org_configuration, native_tokens, token

    body = oauth_claim()
    if org_connector:
        config, _ = org_configuration(tmp_path, body)
        native_tokens(monkeypatch, lambda _: httpx2.Response(200, json={"access_token": token(), "expires_in": 300}))
        runtime_id = None
        connector_id = str(config.runtime_secrets.connector_id)
        root = BASE + f"/integrations/secret-store-connectors/{connector_id}/operations"
    else:
        config = configuration(tmp_path, body)
        runtime_id, connector_id = str(uuid4()), None
        root = BASE + f"/integrations/secret-operations/{body['operation_id']}"
    client = RegistryApiClient(config.registry_api)
    client.runtime_client_id = runtime_id
    statuses = []

    def negotiated(request):
        # Same mandatory API boundary: no OAuth material/dispatch without negotiation.
        if "secret-store.oauth.v1" not in request.headers.get("X-LightNow-Runtime-Capabilities", "").split(","):
            statuses.append(409)
            return httpx.Response(409, json={"error": {"code": "SECRET_STORE_OAUTH_EXECUTOR_UPGRADE_REQUIRED"}})
        assert request.headers["Authorization"].startswith("Bearer ")
        assert "X-Tenant" not in request.headers
        statuses.append(200)
        return httpx.Response(200, json={"data": body} if request.url.path.endswith("/claim") else receipt(body))

    result_url = root + (f"/{body['operation_id']}/result" if org_connector else "/result")
    with respx.mock as transport:
        claimed = transport.post(root + "/claim").mock(side_effect=negotiated)
        reported = transport.post(result_url).mock(side_effect=negotiated)
        writes = []
        for role in ("access", "grant"):
            writes.append(transport.post(url(body, role)).respond(200, json={"data": {"version": 2}}))
            transport.get(url(body, role), params={"version": 2}).respond(
                200, json=kv(body["write"][role], body["operation_id"]))
        identity, _ = await client.secret_worker_authorization(connector_id, runtime_id)
        with pytest.raises(httpx.HTTPStatusError) as denied:
            await client.secret_operation_request(root.removeprefix(BASE) + "/claim", {}, identity,
                                                  connector_id=connector_id, runtime_client_id=runtime_id)
        assert denied.value.response.status_code == 409  # Bare clients never gain worker capability.
        assert not any(route.called for route in writes)
        instance = SecretStoreWorker(config, client)
        await instance._run(None if org_connector else metadata(body))
    assert statuses == [409, 200, 200]
    assert claimed.call_count == 2 and reported.call_count == 1
    assert all(route.call_count == 1 for route in writes)
    for call in (claimed.calls[-1], reported.calls[0]):
        assert call.request.headers["X-LightNow-Runtime-Capabilities"] == ",".join(client.runtime_capabilities())
    with instance.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests WHERE terminal_at IS NULL").fetchone()[0] == 0
