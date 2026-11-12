from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
import respx
from pydantic import ValidationError

from lightnow_proxy.config import RuntimeSecretProviderConfig, RuntimeSecretsConfig
from lightnow_proxy.runtime_secrets import RuntimeSecretResolver
from lightnow_proxy.secret_operations import SecretOperation, SecretOperationExecutor


def claim(action="kv2.write") -> dict:
    operation, provider, binding = map(str, (uuid4(), uuid4(), uuid4()))
    return {
        "operation_id": operation,
        "claim": {"id": str(uuid4()), "revision": 1, "action": action,
                  "expires_at": (datetime.now(UTC) + timedelta(seconds=50)).isoformat()},
        "provider": {"id": provider, "provider_type": "vault_kv_v2", "resolution_mode": "runtime",
                     "config": {"address": "https://private.vault.example", "mount": "secret", "base_path": "mcp/org",
                                "namespace": "company", "timeout": 5.0}},
        "target": {"path": f"secret/data/mcp/org/{binding}/{operation}", "field": "value", "cas": 1,
                   "expected_version": 2, "operation_marker": operation},
        **({"write": {"value": "owned-κλειδί"}} if action == "kv2.write" else {}),
    }


def executor(body: dict) -> SecretOperationExecutor:
    return SecretOperationExecutor(RuntimeSecretResolver(RuntimeSecretsConfig(providers={
        body["provider"]["id"]: RuntimeSecretProviderConfig(vault_proxy_url="http://127.0.0.1:18200"),
    })))


def endpoint(body: dict) -> str:
    return "http://127.0.0.1:18200/v1/" + body["target"]["path"]


def readback(body: dict, **overrides) -> dict:
    return {"data": {"data": {"value": "owned-κλειδί", "_lightnow_operation_id": body["operation_id"], **overrides},
                     "metadata": {"version": 2, "destroyed": False, "deletion_time": ""}}}


@pytest.mark.asyncio
async def test_writes_once_with_cas_then_attests_only_exact_version_value_and_marker():
    body = claim()
    with respx.mock as router:
        write = router.post(endpoint(body)).respond(200, json={"data": {"version": 2}})
        read = router.get(endpoint(body), params={"version": 2}).respond(200, json=readback(body))
        result = await executor(body).execute(SecretOperation.model_validate(body))
    assert write.call_count == read.call_count == 1
    assert json.loads(write.calls.last.request.content) == {
        "options": {"cas": 1}, "data": {"value": "owned-κλειδί", "_lightnow_operation_id": body["operation_id"]},
    }
    assert write.calls.last.request.headers["X-Vault-Namespace"] == "company"
    assert "X-Vault-Token" not in write.calls.last.request.headers
    assert result == {"claim_id": body["claim"]["id"], "claim_revision": 1, "status": "succeeded", "kv": {
        "version": 2, "operation_marker": body["operation_id"],
        "value_sha256": hashlib.sha256("owned-κλειδί".encode()).hexdigest(),
    }}
    assert "owned-κλειδί" not in repr(result)
    assert "owned-κλειδί" not in repr(SecretOperation.model_validate(body))
    assert "owned-κλειδί" not in SecretOperation.model_validate(body).model_dump_json()


@pytest.mark.asyncio
async def test_reconciliation_only_reads_never_posts_even_if_value_is_missing():
    body = claim("kv2.readback")
    with respx.mock as router:
        read = router.get(endpoint(body), params={"version": 2}).respond(404)
        result = await executor(body).execute(SecretOperation.model_validate(body))
        assert all(call.request.method == "GET" for call in router.calls)
    assert read.call_count == 1
    assert result["status"] == "outcome_unknown"
    assert result["error_code"] == "VAULT_IO_UNCONFIRMED"


@pytest.mark.asyncio
async def test_reads_a_previous_pinned_path_for_cas_replacement_and_ignores_newer_versions():
    body = claim("kv2.readback")
    body["target"]["path"] = body["target"]["path"].rsplit("/", 1)[0] + "/" + str(uuid4())
    with respx.mock as router:
        read = router.get(endpoint(body), params={"version": 2}).respond(200, json=readback(body))
        result = await executor(body).execute(SecretOperation.model_validate(body))
    assert result["status"] == "succeeded"
    assert read.calls.last.request.url.params["version"] == "2"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload,code", [
    (403, {}, "VAULT_ACL_DENIED"),
    (400, {"errors": ["check-and-set parameter did not match the current version"]}, "VAULT_CAS_CONFLICT"),
])
async def test_definite_write_refusals_are_not_retried_and_do_not_activate(status, payload, code):
    body = claim()
    with respx.mock as router:
        write = router.post(endpoint(body)).respond(status, json=payload)
        result = await executor(body).execute(SecretOperation.model_validate(body))
        assert len(router.calls) == 1
    assert write.call_count == 1
    assert result == {"claim_id": body["claim"]["id"], "claim_revision": 1, "status": "failed", "error_code": code}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["lost-response", "redirect", "wrong-version", "oversized", "invalid-json", "server-error"])
async def test_unknown_write_outcomes_never_retry_or_report_success(reply):
    body = claim()
    with respx.mock as router:
        route = router.post(endpoint(body))
        if reply == "lost-response":
            route.side_effect = httpx.ReadTimeout("fixture may contain confidential data")
        elif reply == "redirect":
            route.respond(307, headers={"Location": "http://169.254.169.254/metadata"})
        elif reply == "wrong-version":
            route.respond(200, json={"data": {"version": 3}})
        elif reply == "oversized":
            route.respond(200, content=b"x" * 65537)
        elif reply == "invalid-json":
            route.respond(200, content=b"not json")
        else:
            route.respond(503)
        result = await executor(body).execute(SecretOperation.model_validate(body))
        assert len(router.calls) == 1
    assert result["status"] == "outcome_unknown"
    assert result["error_code"] == "VAULT_IO_UNCONFIRMED"
    assert "fixture" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["value", "marker", "metadata", "deleted", "destroyed", "bool-version", "nonstring-value"])
async def test_write_success_alone_does_not_attest_unconfirmed_readback(defect):
    body = claim()
    payload = readback(body)
    if defect == "value":
        payload["data"]["data"]["value"] = "another value"
    elif defect == "marker":
        payload["data"]["data"]["_lightnow_operation_id"] = str(uuid4())
    elif defect == "metadata":
        payload["data"]["metadata"]["version"] = 3
    elif defect == "deleted":
        payload["data"]["metadata"]["deletion_time"] = "2026-11-04T00:00:00Z"
    elif defect == "destroyed":
        payload["data"]["metadata"]["destroyed"] = True
    elif defect == "bool-version":
        payload["data"]["metadata"]["version"] = True
    else:
        payload["data"]["data"]["value"] = 42
    with respx.mock as router:
        router.post(endpoint(body)).respond(200, json={"data": {"version": 2}})
        router.get(endpoint(body), params={"version": 2}).respond(200, json=payload)
        result = await executor(body).execute(SecretOperation.model_validate(body))
        assert len(router.calls) == 2
    assert result["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_requires_an_explicit_local_write_mapping_without_default_or_descriptor_fallback():
    body = claim()
    worker = SecretOperationExecutor(RuntimeSecretResolver(RuntimeSecretsConfig()))
    with respx.mock as router:
        result = await worker.execute(SecretOperation.model_validate(body))
        assert not router.calls
    assert result["status"] == "failed"
    assert result["error_code"] == "PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_keyring_writes_require_a_local_address_override_not_remote_provider_address(monkeypatch):
    body = claim()
    resolver = RuntimeSecretResolver(RuntimeSecretsConfig(providers={body["provider"]["id"]: RuntimeSecretProviderConfig(auth_mode="keyring")}))
    async def must_not_read_keyring(*args):
        pytest.fail("Missing local write enrollment must fail before reading a bootstrap token")
    monkeypatch.setattr(resolver, "_keyring_token", must_not_read_keyring)
    with respx.mock as router:
        result = await SecretOperationExecutor(resolver).execute(SecretOperation.model_validate(body))
        assert not router.calls
    assert result["error_code"] == "PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [-1, 120])
async def test_never_performs_vault_io_for_expired_or_unbounded_claim(seconds):
    body = claim()
    body["claim"]["expires_at"] = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
    with respx.mock as router:
        result = await executor(body).execute(SecretOperation.model_validate(body))
        assert not router.calls
    assert result["status"] == "outcome_unknown"


@pytest.mark.parametrize("defect", ["cas-bool", "version-bool", "cas-mismatch", "marker-mismatch", "path-traversal", "path-query", "foreign-prefix", "readback-with-value", "write-no-value", "extra-field"])
def test_rejects_invalid_or_overbroad_claims_before_vault_io(defect):
    body = claim()
    if defect == "cas-bool":
        body["target"]["cas"] = True
    elif defect == "version-bool":
        body["target"]["expected_version"] = True
    elif defect == "cas-mismatch":
        body["target"]["expected_version"] = 3
    elif defect == "marker-mismatch":
        body["target"]["operation_marker"] = str(uuid4())
    elif defect == "path-traversal":
        body["target"]["path"] = "secret/data/../metadata/private"
    elif defect == "path-query":
        body["target"]["path"] += "?version=3"
    elif defect == "foreign-prefix":
        body["target"]["path"] = body["target"]["path"].replace("mcp/org", "mcp/foreign")
    elif defect == "readback-with-value":
        body["claim"]["action"] = "kv2.readback"
    elif defect == "write-no-value":
        del body["write"]
    else:
        body["command"] = "arbitrary"
    with pytest.raises(ValidationError):
        SecretOperation.model_validate(body)


@pytest.mark.asyncio
async def test_claim_deadline_cancels_a_slow_vault_stream_instead_of_waiting_for_its_next_chunk():
    body = claim("kv2.readback")
    body["claim"]["expires_at"] = (datetime.now(UTC) + timedelta(seconds=.1)).isoformat()
    observed = {"canceled": False, "yielded": False}

    class SlowResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                await asyncio.sleep(.5)
                observed["yielded"] = True
                yield json.dumps(readback(body)).encode()
            except asyncio.CancelledError:
                observed["canceled"] = True
                raise

    with respx.mock as router:
        router.get(endpoint(body), params={"version": 2}).respond(200, stream=SlowResponse())
        result = await executor(body).execute(SecretOperation.model_validate(body))
        assert len(router.calls) == 1
    assert result["status"] == "outcome_unknown"
    assert observed == {"canceled": True, "yielded": False}


@pytest.mark.asyncio
async def test_claim_deadline_also_bounds_local_bootstrap_before_any_vault_request(monkeypatch):
    body = claim()
    body["claim"]["expires_at"] = (datetime.now(UTC) + timedelta(seconds=.05)).isoformat()
    worker = executor(body)
    observed = {"canceled": False}

    async def slow_bootstrap(*args, **kwargs):
        try:
            await asyncio.sleep(.5)
            pytest.fail("Expired bootstrap must not create a Vault connection")
        except asyncio.CancelledError:
            observed["canceled"] = True
            raise

    monkeypatch.setattr(worker.resolver, "vault_connection", slow_bootstrap)
    with respx.mock as router:
        result = await worker.execute(SecretOperation.model_validate(body))
        assert not router.calls
    assert result["status"] == "outcome_unknown"
    assert observed["canceled"]
