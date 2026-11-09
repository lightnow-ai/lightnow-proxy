from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import zlib
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import httpx2
import pytest
import respx
from pydantic import ValidationError

from lightnow_proxy.secret_operations import OAuthOperation, OAuthOperationExecutor
import lightnow_proxy.secret_operations as operations
from test_secret_operations import claim, executor


def oauth_claim(action="oauth.write"):
    body = claim()
    target = body.pop("target")
    body.pop("write")
    body.update(schema="secret-store.oauth.v1", metadata={
        "endpoint": "https://mcp.example/mcp", "issuer": "https://oauth.example",
        "expiresAt": 2_000_000_000, "obtainedAt": 1_900_000_000, "reconnect": False,
    }, targets={"access": target, "grant": {**target, "path": target["path"].rsplit("/", 1)[0]+"/"+str(uuid4())}},
        grant_reference=None)
    body["claim"]["action"] = action
    if action == "oauth.write":
        body["write"] = {"access": "Bearer initial-access", "grant": json.dumps(grant(body))}
    if action == "oauth.refresh":
        body["grant_reference"] = {"path": body["targets"]["grant"]["path"], "field": "value",
                                   "version": 1, "operation_id": str(uuid4())}
    return body


def grant(body, secret="initial-refresh"):
    return {"options": {"clientId": "own-client", "clientSecret": "own-client-secret",
                        "redirectUri": "https://ui.example/profiles/oauth/callback",
                        "urlAccessToken": "https://oauth.example/token", "urlAuthorize": "https://oauth.example/authorize",
                        "tokenAuthMethod": "client_secret_basic", "scopes": ["offline_access"]},
            "issuer": body["metadata"]["issuer"], "refreshToken": secret, "metadata": dict(body["metadata"])}


def url(body, role):
    return "http://127.0.0.1:18200/v1/"+body["targets"][role]["path"]


def kv(value, marker, version=2, **metadata):
    return {"data": {"data": {"value": value, "_lightnow_operation_id": marker},
                     "metadata": {"version": version, "destroyed": False, "deletion_time": "", **metadata}}}


def oauth_executor(body):
    return OAuthOperationExecutor(executor(body).resolver)


def token_transport(monkeypatch, handler):
    native = operations.BoundedOAuth2Client
    requests = []
    def capture(request):
        requests.append(request)
        return handler(request)
    monkeypatch.setattr(operations, "BoundedOAuth2Client",
                        lambda *args, **kwargs: native(*args, **kwargs, transport=httpx2.MockTransport(capture)))
    return requests


class TokenResponseStream(httpx2.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def token_stream_response(chunks, compressed):
    if compressed:
        compressor = zlib.compressobj(wbits=31)
        chunks = [compressor.compress(chunk) + compressor.flush(zlib.Z_SYNC_FLUSH) for chunk in chunks]
        chunks[-1] += compressor.flush(zlib.Z_FINISH)
    stream = TokenResponseStream(chunks)
    return httpx2.Response(200, headers={"Content-Encoding": "gzip"} if compressed else {}, stream=stream), stream


@pytest.mark.asyncio
async def test_oauth_write_atomically_attests_separate_roles_without_exposing_material():
    body = oauth_claim()
    with respx.mock as transport:
        for role in ("access", "grant"):
            transport.post(url(body, role)).respond(200, json={"data": {"version": 2}})
            transport.get(url(body, role), params={"version": 2}).respond(200, json=kv(body["write"][role], body["operation_id"]))
        result = await oauth_executor(body).execute(OAuthOperation.model_validate(body))
        writes = [call for call in transport.calls if call.request.method == "POST"]
    assert result["status"] == "succeeded" and "kv" not in result
    assert len(writes) == 2
    for role in ("access", "grant"):
        assert result["oauth"][role] == {"version": 2, "operation_marker": body["operation_id"],
                                         "value_sha256": hashlib.sha256(body["write"][role].encode()).hexdigest()}
    assert result["oauth"]["expires_at"] == body["metadata"]["expiresAt"]
    assert "initial-access" not in repr(result) and "initial-refresh" not in repr(result)
    assert "initial-refresh" not in OAuthOperation.model_validate(body).model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("rotated", [True, False])
@pytest.mark.parametrize("compressed", [True, False])
async def test_native_authlib_refresh_preserves_resource_clientauth_and_missing_refresh_token(rotated, compressed, monkeypatch):
    body = oauth_claim("oauth.refresh")
    body["metadata"]["hasRefreshToken"] = True
    saved = {}
    response, stream = token_stream_response([json.dumps({
        "access_token": "rotated-access", "token_type": "Bearer", "expires_in": 3600,
        **({"refresh_token": "rotated-refresh"} if rotated else {}),
    }).encode()], compressed)
    token = token_transport(monkeypatch, lambda _: response)
    def write(role):
        def capture(request):
            data = json.loads(request.content)
            assert data["options"] == {"cas": 1}
            saved[role] = data["data"]["value"]
            return httpx.Response(200, json={"data": {"version": 2}})
        return capture
    with respx.mock as transport:
        reference = body["grant_reference"]
        transport.get(url(body, "grant"), params={"version": 1}).respond(
            200, json=kv(json.dumps(grant(body)), reference["operation_id"], 1))
        for role in ("access", "grant"):
            transport.post(url(body, role)).mock(side_effect=write(role))
            transport.get(url(body, role), params={"version": 2}).mock(
                side_effect=lambda _request, role=role: httpx.Response(200, json=kv(saved[role], body["operation_id"])))
        result = await oauth_executor(body).execute(OAuthOperation.model_validate(body))
    assert result["status"] == "succeeded"
    assert len(token) == 1
    assert stream.closed and stream.consumed == 1
    assert token[0].headers["Authorization"].startswith("Basic ")
    assert parse_qs(token[0].content.decode()) == {
        "grant_type": ["refresh_token"], "refresh_token": ["initial-refresh"], "resource": [body["metadata"]["endpoint"]],
        "redirect_uri": ["https://ui.example/profiles/oauth/callback"],
    }
    next_grant = json.loads(saved["grant"])
    assert saved["access"] == "Bearer rotated-access"
    assert next_grant["refreshToken"] == ("rotated-refresh" if rotated else "initial-refresh")
    assert "accessToken" not in next_grant
    assert next_grant["metadata"]["endpoint"] == body["metadata"]["endpoint"]
    assert next_grant["metadata"]["hasRefreshToken"] is True
    assert result["oauth"]["expires_at"] == next_grant["metadata"]["expiresAt"]
    assert result["oauth"]["expires_at"] > int(datetime.now(UTC).timestamp())


@pytest.mark.asyncio
@pytest.mark.parametrize("compressed", [True, False])
async def test_oversized_native_oauth_stream_stops_before_remaining_body_and_never_writes(compressed, monkeypatch):
    body = oauth_claim("oauth.refresh")
    response, stream = token_stream_response(
        [b" " * 8192] * 9 + [b'{"access_token":"must-not-be-used"}'], compressed,
    )
    if compressed:
        assert sum(map(len, stream.chunks)) < operations.RuntimeSecretResolver.MAX_RESPONSE_BYTES
    token = token_transport(monkeypatch, lambda _: response)
    with respx.mock as transport:
        reference = body["grant_reference"]
        transport.get(url(body, "grant"), params={"version": 1}).respond(
            200, json=kv(json.dumps(grant(body)), reference["operation_id"], 1))
        result = await oauth_executor(body).execute(OAuthOperation.model_validate(body))
        assert len(transport.calls) == 1  # Only the pinned grant read; no partial Vault writes.
    assert len(token) == 1 and stream.closed
    assert stream.consumed < len(stream.chunks)
    assert result["status"] == "outcome_unknown" and result["error_code"] == "VAULT_IO_UNCONFIRMED"
    assert "must-not-be-used" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status,code", [
    ("invalid_grant", 400, "OAUTH_RECONNECT_REQUIRED"),
    ("temporarily_unavailable", 503, "OAUTH_PROVIDER_TEMPORARY"),
    ("lost", 0, "VAULT_IO_UNCONFIRMED"),
])
async def test_provider_failure_never_replays_refresh_or_writes_a_partial_pair(error, status, code, monkeypatch):
    body = oauth_claim("oauth.refresh")
    def failure(_request):
        if status:
            return httpx2.Response(status, json={"error": error})
        raise httpx2.ReadError("lost after rotation")
    token = token_transport(monkeypatch, failure)
    with respx.mock as transport:
        ref = body["grant_reference"]
        transport.get(url(body, "grant"), params={"version": 1}).respond(200, json=kv(json.dumps(grant(body)), ref["operation_id"], 1))
        result = await oauth_executor(body).execute(OAuthOperation.model_validate(body))
        assert len(transport.calls) == 1
    assert len(token) == 1
    assert result["error_code"] == code
    assert result["status"] == ("outcome_unknown" if error == "lost" else "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["access", "grant", None])
async def test_oauth_readback_is_get_only_even_with_partial_pair_or_lost_rotation_response(missing):
    body = oauth_claim("oauth.readback")
    values = {"access": "Bearer recovered-access", "grant": json.dumps(grant(body))}
    with respx.mock as transport:
        for role in ("access", "grant"):
            route = transport.get(url(body, role), params={"version": 2})
            if role == missing:
                route.respond(404)
            else:
                route.respond(200, json=kv(values[role], body["operation_id"]))
        result = await oauth_executor(body).execute(OAuthOperation.model_validate(body))
        assert all(call.request.method == "GET" for call in transport.calls)
    assert result["status"] == ("succeeded" if missing is None else "outcome_unknown")


@pytest.mark.asyncio
async def test_second_role_acl_denial_does_not_activate_or_retry_first_role():
    body = oauth_claim()
    with respx.mock as transport:
        access = transport.post(url(body, "access")).respond(200, json={"data": {"version": 2}})
        transport.get(url(body, "access"), params={"version": 2}).respond(200, json=kv(body["write"]["access"], body["operation_id"]))
        denied = transport.post(url(body, "grant")).respond(403)
        result = await oauth_executor(body).execute(OAuthOperation.model_validate(body))
    assert access.call_count == denied.call_count == 1
    assert result["status"] == "failed" and result["error_code"] == "VAULT_ACL_DENIED"
    assert "oauth" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", [True, False])
async def test_access_only_provider_can_connect_but_requires_reconnect_before_renewal(refresh):
    body = oauth_claim("oauth.refresh" if refresh else "oauth.write")
    body["metadata"]["hasRefreshToken"] = False
    encoded = json.dumps(grant(body, None))
    if not refresh:
        body["write"]["grant"] = encoded
    with respx.mock as transport:
        if refresh:
            transport.get(url(body, "grant"), params={"version": 1}).respond(
                200, json=kv(encoded, body["grant_reference"]["operation_id"], 1))
        else:
            for role in ("access", "grant"):
                transport.post(url(body, role)).respond(200, json={"data": {"version": 2}})
                transport.get(url(body, role), params={"version": 2}).respond(200, json=kv(body["write"][role], body["operation_id"]))
        result = await oauth_executor(body).execute(OAuthOperation.model_validate(body))
    assert result["status"] == ("failed" if refresh else "succeeded")
    if refresh:
        assert result["error_code"] == "OAUTH_RECONNECT_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["none", "client_secret_post"])
async def test_native_refresh_uses_the_recorded_public_or_post_client_authentication(method, monkeypatch):
    body = oauth_claim("oauth.refresh")
    previous = grant(body)
    previous["options"]["tokenAuthMethod"] = method
    previous["options"]["clientSecret"] = "" if method == "none" else "own-post-secret"
    token = token_transport(monkeypatch, lambda _: httpx2.Response(400, json={"error": "invalid_grant"}))
    with respx.mock as transport:
        transport.get(url(body, "grant"), params={"version": 1}).respond(
            200, json=kv(json.dumps(previous), body["grant_reference"]["operation_id"], 1))
        result = await oauth_executor(body).execute(OAuthOperation.model_validate(body))
    assert result["error_code"] == "OAUTH_RECONNECT_REQUIRED" and len(token) == 1
    data = parse_qs(token[0].content.decode())
    assert data["client_id"] == ["own-client"] and "Authorization" not in token[0].headers
    assert (data.get("client_secret") == ["own-post-secret"]) is (method == "client_secret_post")


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["marker", "version", "destroyed", "deleted", "issuer", "endpoint"])
async def test_prior_grant_binding_and_pinned_version_are_checked_before_any_refresh(mutation, monkeypatch):
    body = oauth_claim("oauth.refresh")
    previous = grant(body)
    if mutation in ("issuer", "endpoint"):
        previous["metadata"][mutation] = "https://foreign.example"
    read = kv(json.dumps(previous), body["grant_reference"]["operation_id"], 1)
    if mutation == "marker":
        read["data"]["data"]["_lightnow_operation_id"] = str(uuid4())
    elif mutation == "version":
        read["data"]["metadata"]["version"] = 2
    elif mutation == "destroyed":
        read["data"]["metadata"]["destroyed"] = True
    elif mutation == "deleted":
        read["data"]["metadata"]["deletion_time"] = "2026-11-04T00:00:00Z"
    token = token_transport(monkeypatch, lambda _: pytest.fail("Unconfirmed grant must not refresh"))
    with respx.mock as transport:
        transport.get(url(body, "grant"), params={"version": 1}).respond(200, json=read)
        result = await oauth_executor(body).execute(OAuthOperation.model_validate(body))
        assert len(transport.calls) == 1
    assert result["status"] == "outcome_unknown" and not token


@pytest.mark.parametrize("change", ["same-path", "foreign-binding", "wrong-cas", "wrong-marker", "missing-grant", "refresh-with-values"])
def test_strict_pair_claims_reject_role_scope_and_action_substitutions(change):
    body = oauth_claim("oauth.refresh")
    if change == "same-path":
        body["targets"]["access"]["path"] = body["targets"]["grant"]["path"]
    elif change == "foreign-binding":
        target = body["targets"]["access"]
        parts = target["path"].rsplit("/", 2)
        target["path"] = parts[0]+"/"+str(uuid4())+"/"+parts[2]
    elif change == "wrong-cas":
        body["targets"]["access"]["cas"] = 2
    elif change == "wrong-marker":
        body["targets"]["grant"]["operation_marker"] = str(uuid4())
    elif change == "missing-grant":
        body["grant_reference"] = None
    else:
        body["write"] = {"access": "Bearer forbidden", "grant": "forbidden"}
    with pytest.raises(ValidationError):
        OAuthOperation.model_validate(body)


@pytest.mark.parametrize("has_refresh", [True, False])
def test_oauth_metadata_carries_only_boolean_refresh_presence(has_refresh):
    body = oauth_claim()
    body["metadata"]["hasRefreshToken"] = has_refresh
    body["write"]["grant"] = json.dumps(grant(body, "initial-refresh" if has_refresh else None))
    operation = OAuthOperation.model_validate(body)
    assert operation.metadata.hasRefreshToken is has_refresh


@pytest.mark.parametrize("invalid", ["false", 0, 1, [], {}])
def test_oauth_metadata_rejects_coerced_refresh_presence(invalid):
    body = oauth_claim()
    body["metadata"]["hasRefreshToken"] = invalid
    with pytest.raises(ValidationError):
        OAuthOperation.model_validate(body)
