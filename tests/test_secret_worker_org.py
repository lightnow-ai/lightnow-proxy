from __future__ import annotations

import hashlib
import json
from time import time
from urllib.parse import parse_qs
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import httpx2
import jwt
import pytest
import respx

from lightnow_proxy.config import ProxyConfig
from lightnow_proxy.registry import RegistryApiClient
from lightnow_proxy.secret_worker import SecretStoreWorker
from test_secret_operations import claim, endpoint, readback
from test_secret_worker import receipt, replay

BASE = "https://registry.test/v0.1"
ISSUER = "https://identity.test/realms/own"
TOKEN = ISSUER + "/protocol/openid-connect/token"


def configuration(tmp_path, operation):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = tmp_path / "connector.pem"
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_file.chmod(0o600)
    return ProxyConfig.model_validate({
        "auth": {"enabled": False, "issuer": ISSUER}, "profiles": {"worker": {}}, "upstreams": {},
        "registry_api": {"enabled": True, "base_url": BASE, "token_url": TOKEN, "expected_issuer": ISSUER,
                         "client_id": "own-connector", "client_private_key_file": str(key_file),
                         "client_key_id": "own-key", "client_scope": "secret-store.operations.v1"},
        "runtime_secrets": {"worker_enabled": True, "connector_id": str(uuid4()),
                            "journal_path": str(tmp_path / "worker" / "operations.sqlite3"),
                            "providers": {operation["provider"]["id"]: {"vault_proxy_url": "http://127.0.0.1:18200"}}},
    }), key


def native_tokens(monkeypatch, handler):
    """Exercise native Authlib signing with its installed HTTPX transport."""
    import lightnow_proxy.registry as registry_module
    native_client = registry_module.AsyncOAuth2Client
    calls = []

    def exchange(request):
        calls.append(request)
        return handler(request)

    def isolated_client(*args, **kwargs):
        return native_client(*args, **kwargs, transport=httpx2.MockTransport(exchange))

    monkeypatch.setattr(registry_module, "AsyncOAuth2Client", isolated_client)
    return calls


def token(**overrides):
    return jwt.encode({"iss": ISSUER, "sub": "dedicated-service-account", "azp": "own-connector",
                       "scope": "secret-store.operations.v1", "exp": time()+300, **overrides}, "owned-unit-key-with-at-least-32-bytes", algorithm="HS256")


@pytest.mark.asyncio
async def test_org_pool_uses_native_private_key_jwt_and_rotates_nonce_only_after_confirmed_receipt(tmp_path, monkeypatch):
    body = claim()
    config, key = configuration(tmp_path, body)
    client = RegistryApiClient(config.registry_api)
    worker = SecretStoreWorker(config, client)
    root = BASE + f"/integrations/secret-store-connectors/{config.runtime_secrets.connector_id}/operations"
    requests = []
    def authenticate(request):
        sent = parse_qs(request.content.decode())
        assert sent["grant_type"] == ["client_credentials"]
        assert "client_secret" not in sent and "password" not in sent
        assertion = sent["client_assertion"][0]
        proof = jwt.decode(assertion, key.public_key(), algorithms=["RS256"], audience=TOKEN)
        assert proof["sub"] == proof["iss"] == "own-connector"
        assert jwt.get_unverified_header(assertion)["kid"] == "own-key"
        return httpx2.Response(200, json={"access_token": token(), "expires_in": 300})
    def claimed(request):
        value = json.loads(request.content)
        assert "runtime_client_id" not in value and "X-Tenant" not in request.headers
        requests.append(value["request_id"])
        identity = (BASE, ISSUER, "dedicated-service-account", "own-connector", "org_connector", str(config.runtime_secrets.connector_id))
        namespace = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        assert worker.journal.pending(namespace)[0]["request_id"] == value["request_id"]
        return httpx.Response(200, json={"data": body if len(requests) == 1 else None})
    auth = native_tokens(monkeypatch, authenticate)
    with respx.mock as transport:
        transport.post(root + "/claim").mock(side_effect=claimed)
        written = transport.post(endpoint(body)).respond(200, json={"data": {"version": 2}})
        transport.get(endpoint(body), params={"version": 2}).respond(200, json=readback(body))
        reported = transport.post(root + f"/{body['operation_id']}/result").respond(200, json=receipt(body))
        await worker._run()
        await worker._run()
    assert len(auth) == 1 and written.call_count == reported.call_count == 1
    assert len(requests) == 2 and requests[0] != requests[1]
    assert not config.registry_api.use_cli_session and config.registry_api.client_secret is None
    assert "value_sha256" not in worker.journal.path.read_text(errors="replace")


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"iss": "https://foreign.test"}, {"azp": "human-browser"}, {"scope": "openid"}])
async def test_org_worker_refuses_wrong_machine_binding_before_claim(tmp_path, monkeypatch, changed):
    body = claim()
    config, _ = configuration(tmp_path, body)
    worker = SecretStoreWorker(config, RegistryApiClient(config.registry_api))
    auth = native_tokens(monkeypatch, lambda _: httpx2.Response(200, json={"access_token": token(**changed), "expires_in": 300}))
    with respx.mock as transport:
        claimed = transport.post(url__regex=BASE + "/integrations/secret-store-connectors/.*/operations/claim").respond(200)
        await worker._run()
    assert len(auth) == 1 and claimed.call_count == 0 and not worker.active
    with worker.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 409, 503])
async def test_org_rejected_or_unknown_claim_never_rotates_pool_nonce(tmp_path, monkeypatch, status):
    body = claim()
    config, _ = configuration(tmp_path, body)
    worker = SecretStoreWorker(config, RegistryApiClient(config.registry_api))
    requests = []
    def rejected(request):
        requests.append(json.loads(request.content)["request_id"])
        return httpx.Response(status, json={"error": {"code": "BLOCKED"}})
    auth = native_tokens(monkeypatch, lambda _: httpx2.Response(200, json={"access_token": token(), "expires_in": 300}))
    with respx.mock as transport:
        transport.post(url__regex=BASE + "/integrations/secret-store-connectors/.*/operations/claim").mock(side_effect=rejected)
        await worker._run()
        await worker._run()
    assert len(auth) == 1 and len(requests) == 2 and requests[0] == requests[1]


def test_cli_worker_mode_is_outbound_only_and_cannot_start_public_mcp_server(tmp_path, monkeypatch):
    import lightnow_proxy.main as cli
    body = claim()
    config, _ = configuration(tmp_path, body)
    ran = []
    async def run(loaded):
        ran.append(loaded.runtime_secrets.connector_id)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "run_secret_store_worker", run)
    monkeypatch.setattr(cli, "create_app", lambda *_args: pytest.fail("Headless mode must not create an HTTP app"))
    monkeypatch.setattr(cli.uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("Headless mode must not listen"))
    monkeypatch.setattr("sys.argv", ["lightnow-proxy", "--secret-store-worker"])
    cli.main()
    assert ran == [config.runtime_secrets.connector_id]
    monkeypatch.setattr("sys.argv", ["lightnow-proxy"])
    with pytest.raises(SystemExit, match="2"):
        cli.main()


@pytest.mark.parametrize("extra", [{"use_cli_session": True}, {"client_secret": "not-a-real-secret"}])
def test_org_configuration_cannot_borrow_personal_or_shared_secret_auth(tmp_path, extra):
    config, _ = configuration(tmp_path, claim())
    raw = config.model_dump(mode="json")
    raw["registry_api"].update(extra)
    with pytest.raises(ValueError, match="private_key_jwt"):
        ProxyConfig.model_validate(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", ["claim", "result"])
async def test_org_restart_reuses_durable_pool_nonce_and_only_reads_back_after_lost_response(tmp_path, monkeypatch, lost):
    body = claim()
    config, _ = configuration(tmp_path, body)
    native_tokens(monkeypatch, lambda _: httpx2.Response(200, json={"access_token": token(), "expires_in": 300}))
    root = BASE + f"/integrations/secret-store-connectors/{config.runtime_secrets.connector_id}/operations"
    requests = []
    reports = []

    def claimed(request):
        requests.append(json.loads(request.content)["request_id"])
        if len(requests) == 1 and lost == "claim":
            raise httpx.ReadError("response unavailable")
        return httpx.Response(200, json={"data": body if len(requests) == 1 else replay(body)})

    def reported(request):
        reports.append(json.loads(request.content))
        if len(reports) == 1 and lost == "result":
            raise httpx.ReadError("response unavailable")
        return httpx.Response(200, json=receipt(body))

    with respx.mock as transport:
        transport.post(root + "/claim").mock(side_effect=claimed)
        write = transport.post(endpoint(body)).respond(200, json={"data": {"version": 2}})
        read = transport.get(endpoint(body), params={"version": 2}).respond(200, json=readback(body))
        transport.post(root + f"/{body['operation_id']}/result").mock(side_effect=reported)
        first = SecretStoreWorker(config, RegistryApiClient(config.registry_api))
        await first._run()
        restarted = SecretStoreWorker(config, RegistryApiClient(config.registry_api))
        await restarted._run()
    assert len(requests) == 2 and requests[0] == requests[1]
    assert write.call_count == (1 if lost == "result" else 0)
    assert read.call_count == (2 if lost == "result" else 1)
    with restarted.journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests WHERE terminal_at IS NULL").fetchone()[0] == 0
