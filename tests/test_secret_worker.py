from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import multiprocessing
from pathlib import Path
from time import time
from uuid import uuid4

import httpx
import jwt
import pytest
import respx

from lightnow_proxy.config import ProxyConfig
from lightnow_proxy.registry import RegistryApiClient
from lightnow_proxy.router import ToolRouter
from lightnow_proxy.secret_operations import SecretOperation
from lightnow_proxy.secret_worker import SecretOperationJournal, SecretStoreWorker, SecretWorkerError
from test_secret_operations import claim, endpoint, readback

BASE = "https://registry.test/v0.1"
ISSUER = "https://identity.test/realms/own"


def session(path: Path, *, subject="owner", context="personal"):
    path.write_text(json.dumps({
        "access_token": jwt.encode({"iss": ISSUER, "sub": subject, "azp": "lightnow-cli", "exp": time()+3600}, "own-unit-key-with-at-least-32-bytes", algorithm="HS256"),
        "issuer": ISSUER, "subject": subject, "client_id": "lightnow-cli", "context_type": context,
        **({"context_tenant": str(uuid4())} if context == "tenant" else {}),
    }))


def configuration(tmp_path, operation):
    session(tmp_path / "session.json")
    return ProxyConfig.model_validate({
        "auth": {"enabled": False, "issuer": ISSUER}, "profiles": {"default": {}}, "upstreams": {},
        "registry_api": {"enabled": True, "base_url": BASE, "use_cli_session": True,
                         "cli_session_path": str(tmp_path / "session.json"), "expected_issuer": ISSUER, "expected_subject": "owner"},
        "local_proxy": {"enabled": True, "device_installation_id": str(uuid4()), "client_instance_id": str(uuid4()), "device_hostname": "owned-unit-device"},
        "runtime_secrets": {"worker_enabled": True, "journal_path": str(tmp_path / "worker" / "operations.sqlite3"),
                            "providers": {operation["provider"]["id"]: {"vault_proxy_url": "http://127.0.0.1:18200"}}},
    })


def metadata(operation):
    return [{"operation_id": operation["operation_id"], "status": "queued", "expires_at": (datetime.now(UTC)+timedelta(minutes=10)).isoformat()}]


def receipt(operation, status="succeeded"):
    return {"data": {"id": operation["operation_id"], "connection_id": str(uuid4()), "status": status, "version": 2, "error_code": None}}


def worker(config, runtime_id):
    registry = RegistryApiClient(config.registry_api)
    registry.runtime_client_id = runtime_id
    return SecretStoreWorker(config, registry)


def replay(operation):
    body = json.loads(json.dumps(operation))
    body["claim"]["action"] = "kv2.readback"
    body.pop("write")
    return body


@pytest.mark.asyncio
async def test_heartbeat_installs_actual_worker_and_both_runtime_capabilities_with_durable_request(tmp_path):
    body = claim()
    config = configuration(tmp_path, body)
    router = ToolRouter(config)
    runtime_id = str(uuid4())
    def claimed(request):
        sent = json.loads(request.content)
        with router._secret_worker.journal.connect() as db:
            rows = db.execute("SELECT request_id FROM requests WHERE terminal_at IS NULL").fetchall()
        assert [row[0] for row in rows] == [sent["request_id"]]
        assert sent["runtime_client_id"] == runtime_id
        assert "X-Tenant" not in request.headers
        return httpx.Response(200, json={"data": body})
    with respx.mock as transport:
        heartbeat = transport.post(url__regex=BASE + "/integrations/devices/.*/heartbeat").respond(200, json={
            "device": {"clients": [{"client_instance_id": str(config.local_proxy.client_instance_id), "id": runtime_id}]},
            "secret_store_operations": metadata(body),
        })
        transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/claim").mock(side_effect=claimed)
        written = transport.post(endpoint(body)).respond(200, json={"data": {"version": 2}})
        transport.get(endpoint(body), params={"version": 2}).respond(200, json=readback(body))
        reported = transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/result").respond(200, json=receipt(body))
        profile = transport.get(BASE + "/integrations/profiles/default/servers").respond(200, json={"servers": []})
        assert await router.send_device_heartbeat()
        await router._secret_worker_task
        await router.registry_client._fetch_profile_servers("default", True)
    assert json.loads(heartbeat.calls[0].request.content)["client"]["managed_capabilities"] == ["managed-connections.v1", "secret-store.versioned-read.v1", "secret-store.operations.v1"]
    assert profile.calls[0].request.headers["X-LightNow-Runtime-Capabilities"] == "managed-connections.v1,secret-store.versioned-read.v1,secret-store.operations.v1"
    assert profile.calls[0].request.headers["X-LightNow-Runtime-Client"] == runtime_id
    assert written.call_count == reported.call_count == 1
    saved = Path(config.runtime_secrets.journal_path).read_bytes()
    assert body["write"]["value"].encode() not in saved
    assert hashlib.sha256(body["write"]["value"].encode()).hexdigest().encode() not in saved
    assert Path(config.runtime_secrets.journal_path).stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_lost_result_restart_and_expired_local_clock_keep_the_exact_nonce_without_another_write(tmp_path, monkeypatch, caplog):
    body = claim()
    config = configuration(tmp_path, body)
    runtime_id = str(uuid4())
    first = worker(config, runtime_id)
    requests = []
    def claim_response(request):
        requests.append(json.loads(request.content)["request_id"])
        return httpx.Response(200, json={"data": body if len(requests) == 1 else None})
    with respx.mock as transport:
        transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/claim").mock(side_effect=claim_response)
        written = transport.post(endpoint(body)).respond(200, json={"data": {"version": 2}})
        read = transport.get(endpoint(body), params={"version": 2}).respond(200, json=readback(body))
        transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/result").mock(side_effect=httpx.ReadError("lost response"))
        await first.personal_pending(metadata(body))
        monkeypatch.setattr("lightnow_proxy.secret_worker.time", lambda: time()+7200)
        restarted = worker(config, runtime_id)
        await restarted.personal_pending([])  # Terminal work is absent from the new heartbeat.
    assert len(requests) == 2 and requests[0] == requests[1]
    assert written.call_count == read.call_count == 1
    with restarted.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests WHERE terminal_at IS NULL").fetchone()[0] == 0
    assert body["write"]["value"] not in caplog.text


@pytest.mark.asyncio
async def test_lost_claim_response_replays_only_readback_and_retains_unknown_nonce(tmp_path):
    body = claim()
    instance = worker(configuration(tmp_path, body), str(uuid4()))
    ids = []
    def claimed(request):
        ids.append(json.loads(request.content)["request_id"])
        if len(ids) == 1:
            raise httpx.ReadError("claim response lost")
        return httpx.Response(200, json={"data": replay(body)})
    with respx.mock as transport:
        transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/claim").mock(side_effect=claimed)
        write = transport.post(endpoint(body)).respond(200)
        transport.get(endpoint(body), params={"version": 2}).respond(404)
        result = transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/result").respond(202, json=receipt(body, "outcome_unknown"))
        await instance.personal_pending(metadata(body))
        await instance.personal_pending([])
    assert ids[0] == ids[1] and write.call_count == 0
    assert json.loads(result.calls[0].request.content)["status"] == "outcome_unknown"
    with instance.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests WHERE terminal_at IS NULL").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_saved_write_instruction_is_never_executed_again_even_if_server_repeats_it(tmp_path):
    body = claim()
    config = configuration(tmp_path, body)
    instance = worker(config, str(uuid4()))
    identity, _ = await instance.registry.secret_worker_authorization(None, instance.registry.runtime_client_id)
    namespace = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    row = instance.journal.ensure(namespace, body["operation_id"], time()+600)
    instance.journal.stage(row, SecretOperation.model_validate(body))
    with respx.mock as transport:
        transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/claim").respond(200, json={"data": body})
        write = transport.post(endpoint(body)).respond(200)
        await instance.personal_pending([])
    assert write.call_count == 0
    assert instance.last_error == "SECRET_WORKER_STEP_UNCONFIRMED"


@pytest.mark.asyncio
async def test_context_change_after_claim_blocks_io_and_does_not_create_another_nonce(tmp_path):
    body = claim()
    config = configuration(tmp_path, body)
    instance = worker(config, str(uuid4()))
    def changed(_request):
        session(tmp_path / "session.json", context="tenant")
        return httpx.Response(200, json={"data": body})
    with respx.mock as transport:
        claimed = transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/claim").mock(side_effect=changed)
        write = transport.post(endpoint(body)).respond(200)
        await instance.personal_pending(metadata(body))
        await instance.personal_pending(metadata(body))
    assert claimed.call_count == 1 and write.call_count == 0
    assert not instance.active
    assert instance.registry.runtime_capabilities() == ["managed-connections.v1"]
    with instance.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def _hold(path, namespace, ready, release):
    with SecretOperationJournal(Path(path)).lock(namespace):
        ready.set()
        release.wait(5)


@pytest.mark.asyncio
async def test_parallel_process_lock_prevents_claim_or_journal_race(tmp_path):
    body = claim()
    config = configuration(tmp_path, body)
    instance = worker(config, str(uuid4()))
    identity, _ = await instance.registry.secret_worker_authorization(None, instance.registry.runtime_client_id)
    namespace = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold, args=(config.runtime_secrets.journal_path, namespace, ready, release))
    process.start()
    try:
        assert ready.wait(5)
        with respx.mock as transport:
            claimed = transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/claim").respond(200)
            await instance.personal_pending(metadata(body))
        assert claimed.call_count == 0
    finally:
        release.set()
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0


def test_bounded_journal_only_prunes_confirmed_terminal_requests_after_server_ttl(tmp_path, monkeypatch):
    journal = SecretOperationJournal(tmp_path / "worker" / "operations.sqlite3")
    monkeypatch.setattr(journal, "MAX_ROWS", 2)
    one = journal.ensure("namespace", str(uuid4()), time()+10)
    two = journal.ensure("namespace", str(uuid4()), time()+10)
    monkeypatch.setattr("lightnow_proxy.secret_worker.time", lambda: 10_000_000_000)
    with pytest.raises(SecretWorkerError):
        journal.ensure("namespace", str(uuid4()), 10_000_000_600)
    assert journal.pending("namespace")[0]["request_id"] == one["request_id"]
    journal.finish(two)
    monkeypatch.setattr("lightnow_proxy.secret_worker.time", lambda: 10_000_000_601)
    assert journal.ensure("namespace", str(uuid4()), 10_000_001_200)
    assert journal.pending("namespace")[0]["request_id"] == one["request_id"]


@pytest.mark.asyncio
async def test_tenant_proxy_registers_versioned_reader_without_installing_a_user_write_worker(tmp_path):
    config = configuration(tmp_path, claim()).model_dump(mode="json")
    config["runtime_secrets"]["worker_enabled"] = False
    config["local_proxy"].update(scope_type="tenant", sync_from_lightnow=True)
    session(tmp_path / "session.json", context="tenant")
    tenant_id = json.loads((tmp_path / "session.json").read_text())["context_tenant"]
    router = ToolRouter(ProxyConfig.model_validate(config))
    runtime_id = str(uuid4())
    with respx.mock as transport:
        heartbeat = transport.post(url__regex=BASE + "/integrations/devices/.*/heartbeat").respond(200, json={
            "device": {"clients": [{"client_instance_id": str(router.config.local_proxy.client_instance_id), "id": runtime_id}]},
        })
        profile = transport.get(BASE + "/integrations/profiles/default/servers").respond(200, json={"servers": []})
        assert await router.send_device_heartbeat()
        await router.registry_client._fetch_profile_servers("default", True)
    capabilities = ["managed-connections.v1", "secret-store.versioned-read.v1"]
    assert json.loads(heartbeat.calls[0].request.content)["client"]["managed_capabilities"] == capabilities
    assert profile.calls[0].request.headers["X-LightNow-Runtime-Capabilities"] == ",".join(capabilities)
    assert profile.calls[0].request.headers["X-LightNow-Runtime-Client"] == runtime_id
    assert profile.calls[0].request.headers["X-Tenant"] == tenant_id
    assert router._secret_worker is None and not Path(config["runtime_secrets"]["journal_path"]).exists()


@pytest.mark.asyncio
async def test_canceled_io_keeps_dispatch_and_nonce_for_readback_on_restart(tmp_path):
    body = claim()
    config = configuration(tmp_path, body)
    runtime_id = str(uuid4())
    first = worker(config, runtime_id)
    dispatched = asyncio.Event()
    writes = []
    requests = []

    def claimed(request):
        requests.append(json.loads(request.content)["request_id"])
        return httpx.Response(200, json={"data": body if len(requests) == 1 else replay(body)})

    async def uncertain_write(_request):
        writes.append(True)
        dispatched.set()
        await asyncio.Event().wait()

    with respx.mock as transport:
        transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/claim").mock(side_effect=claimed)
        transport.post(endpoint(body)).mock(side_effect=uncertain_write)
        read = transport.get(endpoint(body), params={"version": 2}).respond(200, json=readback(body))
        result = transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/result").respond(200, json=receipt(body))
        task = asyncio.create_task(first.personal_pending(metadata(body)))
        await asyncio.wait_for(dispatched.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with first.journal.connect() as db:
            assert db.execute("SELECT dispatched FROM requests").fetchone()[0] == 1
        restarted = worker(config, runtime_id)
        await restarted.personal_pending([])
    assert len(requests) == 2 and requests[0] == requests[1]
    assert len(writes) == read.call_count == result.call_count == 1


@pytest.mark.asyncio
async def test_account_change_cannot_reclaim_another_namespace_and_original_session_can_resume(tmp_path):
    body = claim()
    config = configuration(tmp_path, body)
    runtime_id = str(uuid4())
    first = worker(config, runtime_id)
    requests = []

    def claimed(request):
        requests.append(json.loads(request.content)["request_id"])
        raise httpx.ReadError("response unavailable")

    with respx.mock as transport:
        transport.post(BASE + f"/integrations/secret-operations/{body['operation_id']}/claim").mock(side_effect=claimed)
        await first.personal_pending(metadata(body))
        session(tmp_path / "session.json", subject="other-owner")
        changed = worker(config, runtime_id)
        await changed.personal_pending([])
        assert not changed.active and len(requests) == 1
        session(tmp_path / "session.json")
        await worker(config, runtime_id).personal_pending([])
    assert len(requests) == 2 and requests[0] == requests[1]


def test_journal_rejects_existing_public_parent_without_changing_permissions_or_creating_files(tmp_path):
    public = tmp_path / "shared"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(ValueError, match="owned private directory"):
        SecretOperationJournal(public / "operations.sqlite3")
    assert public.stat().st_mode & 0o777 == 0o755
    assert list(public.iterdir()) == []


def test_journal_accepts_existing_owned_private_parent_without_changing_its_mode(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    journal = SecretOperationJournal(private / "operations.sqlite3")
    row = journal.ensure("own-namespace", str(uuid4()), time()+600)
    assert journal.pending("own-namespace")[0]["request_id"] == row["request_id"]
    assert private.stat().st_mode & 0o777 == 0o700
    assert journal.path.stat().st_mode & 0o777 == 0o600


def test_journal_refuses_wrong_owner_without_mutating_the_parent(tmp_path, monkeypatch):
    private = tmp_path / "other-owner"
    private.mkdir(mode=0o700)
    monkeypatch.setattr("lightnow_proxy.secret_worker.os.getuid", lambda: private.stat().st_uid + 1)
    with pytest.raises(ValueError, match="owned private directory"):
        SecretOperationJournal(private / "operations.sqlite3")
    assert private.stat().st_mode & 0o777 == 0o700
    assert list(private.iterdir()) == []
