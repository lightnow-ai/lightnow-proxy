"""Execute one Registry-issued private KV2 claim, without retries or stored values."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import hmac
import json
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import httpx2
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt, model_validator

from lightnow_proxy.runtime_secrets import RuntimeSecretError, RuntimeSecretResolver


class ClaimModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class VaultProviderConfig(ClaimModel):
    address: str
    mount: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    base_path: str = Field(pattern=r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
    namespace: str | None = None
    timeout: float | None = None


class VaultProvider(ClaimModel):
    id: UUID
    provider_type: Literal["vault_kv_v2"]
    resolution_mode: Literal["runtime"]
    config: VaultProviderConfig


class VaultClaim(ClaimModel):
    id: UUID
    revision: StrictInt = Field(ge=1)
    expires_at: AwareDatetime
    action: Literal["kv2.write", "kv2.readback"]


class VaultTarget(ClaimModel):
    path: str
    field: Literal["value"]
    cas: StrictInt = Field(ge=0)
    expected_version: StrictInt = Field(ge=1)
    operation_marker: UUID


class VaultWrite(ClaimModel):
    value: SecretStr


def _validate_target(provider: VaultProvider, target: VaultTarget) -> None:
    prefix = f"{provider.config.mount}/data/{provider.config.base_path}/"
    parts = target.path.removeprefix(prefix).split("/")
    if not target.path.startswith(prefix) or len(parts) != 2:
        raise ValueError("Invalid secret operation target")
    for part in parts:
        if str(UUID(part)) != part or UUID(part).int == 0:
            raise ValueError("Invalid secret operation target")


class SecretOperation(ClaimModel):
    operation_id: UUID
    claim: VaultClaim
    provider: VaultProvider
    target: VaultTarget
    write: VaultWrite | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def exact_operation(self) -> SecretOperation:
        if (self.target.operation_marker != self.operation_id
                or self.target.expected_version != self.target.cas + 1
                or (self.claim.action == "kv2.write") != (self.write is not None)):
            raise ValueError("Invalid secret operation claim binding")
        # Both path IDs are opaque server-owned UUIDs. The final ID may be the
        # original operation when CAS replaces an already pinned reference.
        _validate_target(self.provider, self.target)
        return self


class OAuthClaim(VaultClaim):
    action: Literal["oauth.write", "oauth.refresh", "oauth.readback"]


class OAuthTargets(ClaimModel):
    access: VaultTarget
    grant: VaultTarget


class OAuthWrite(ClaimModel):
    access: SecretStr
    grant: SecretStr


class OAuthMetadata(ClaimModel):
    endpoint: str
    issuer: str
    expiresAt: StrictInt | None
    obtainedAt: StrictInt
    reconnect: bool
    runtimeClientId: str | None = None
    hasRefreshToken: StrictBool | None = None


class OAuthGrantReference(ClaimModel):
    path: str
    field: Literal["value"]
    version: StrictInt = Field(ge=1)
    operation_id: UUID


class OAuthOperation(ClaimModel):
    protocol: Literal["secret-store.oauth.v1"] = Field(alias="schema")
    operation_id: UUID
    claim: OAuthClaim
    provider: VaultProvider
    targets: OAuthTargets
    metadata: OAuthMetadata
    grant_reference: OAuthGrantReference | None
    write: OAuthWrite | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def exact_pair(self) -> OAuthOperation:
        if (self.claim.action == "oauth.write") != (self.write is not None):
            raise ValueError("Invalid OAuth operation material")
        for target in (self.targets.access, self.targets.grant):
            _validate_target(self.provider, target)
            if target.operation_marker != self.operation_id or target.expected_version != target.cas + 1:
                raise ValueError("Invalid OAuth operation target binding")
        if (self.targets.access.path == self.targets.grant.path
                or self.targets.access.path.rsplit("/", 1)[0] != self.targets.grant.path.rsplit("/", 1)[0]):
            raise ValueError("OAuth roles require separate paths in the same binding")
        if self.grant_reference is not None:
            if (self.grant_reference.path != self.targets.grant.path
                    or self.grant_reference.version != self.targets.grant.cas):
                raise ValueError("Invalid prior OAuth grant binding")
        elif self.claim.action == "oauth.refresh":
            raise ValueError("OAuth refresh requires its pinned grant")
        return self


class _VaultRefusal(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class BoundedOAuth2Client(AsyncOAuth2Client):
    """Keep native OAuth authentication while bounding decoded token-response IO."""

    async def send(self, request: httpx2.Request, *, stream: bool = False, **kwargs: Any) -> httpx2.Response:
        response = await super().send(request, stream=True, **kwargs)
        try:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes(chunk_size=8192):
                size += len(chunk)
                if size > RuntimeSecretResolver.MAX_RESPONSE_BYTES:
                    raise RuntimeSecretError("OAuth provider response exceeds its size limit")
                chunks.append(chunk)
            headers = response.headers.copy()
            # aiter_bytes already decoded the native HTTP content encoding.
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx2.Response(response.status_code, headers=headers, content=b"".join(chunks),
                                   request=response.request, extensions=response.extensions, history=response.history)
        finally:
            await response.aclose()


class SecretOperationExecutor:
    """Only attest exact-version readback; Registry retains final authorization."""

    def __init__(self, resolver: RuntimeSecretResolver):
        self.resolver = resolver

    @staticmethod
    def _current(operation: SecretOperation) -> bool:
        remaining = (operation.claim.expires_at - datetime.now(UTC)).total_seconds()
        return 0 < remaining <= 60

    @staticmethod
    def _data(response: httpx.Response, *, writing: bool) -> dict[str, Any]:
        if len(response.content) > RuntimeSecretResolver.MAX_RESPONSE_BYTES:
            raise RuntimeSecretError("Vault operation response exceeded its size limit")
        if writing and response.status_code == 403:
            raise _VaultRefusal("VAULT_ACL_DENIED")
        if writing and response.status_code == 400 and "check-and-set" in response.text:
            raise _VaultRefusal("VAULT_CAS_CONFLICT")
        if not response.is_success:
            raise RuntimeSecretError("Vault operation outcome is unconfirmed")
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeSecretError("Vault operation outcome is unconfirmed") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeSecretError("Vault operation outcome is unconfirmed")
        return data

    async def execute(self, operation: SecretOperation) -> dict[str, Any]:
        base: dict[str, Any] = {"claim_id": str(operation.claim.id), "claim_revision": operation.claim.revision}
        if not self._current(operation):
            return {**base, "status": "outcome_unknown", "error_code": "VAULT_IO_UNCONFIRMED"}
        remaining = (operation.claim.expires_at - datetime.now(UTC)).total_seconds()
        try:
            # HTTPX's per-chunk timeout is not a total operation deadline.
            # Include local bootstrap and both external requests in the claim budget.
            async with asyncio.timeout(remaining):
                return await self._execute_current(operation, base)
        except TimeoutError:
            return {**base, "status": "outcome_unknown", "error_code": "VAULT_IO_UNCONFIRMED"}

    async def _execute_current(self, operation: SecretOperation, base: dict[str, Any]) -> dict[str, Any]:
        # Never use the reader's implicit local Vault Proxy mapping for writes.
        try:
            client, address, headers = await self.resolver.vault_connection(
                str(operation.provider.id), operation.provider.config.model_dump(), require_local=True,
            )
        except (RuntimeSecretError, OSError):
            return {**base, "status": "failed", "error_code": "PROVIDER_UNAVAILABLE"}
        try:
            async with client:
                if not self._current(operation):
                    raise RuntimeSecretError("Vault operation claim expired")
                url = f"{address.rstrip('/')}/v1/{operation.target.path}"
                if operation.write is not None:
                    written = self._data(await self.resolver.vault_request(client, "POST", url, headers=headers, json={
                        "options": {"cas": operation.target.cas},
                        "data": {"value": operation.write.value.get_secret_value(),
                                 "_lightnow_operation_id": str(operation.operation_id)},
                    }), writing=True)
                    if type(written.get("version")) is not int or written["version"] != operation.target.expected_version:
                        raise RuntimeSecretError("Vault write version is unconfirmed")
                if not self._current(operation):
                    raise RuntimeSecretError("Vault operation claim expired")
                data = self._data(await self.resolver.vault_request(client, "GET", url, headers=headers, params={
                    "version": operation.target.expected_version,
                }), writing=False)
                metadata, values = data.get("metadata"), data.get("data")
                if (not isinstance(metadata, dict) or type(metadata.get("version")) is not int
                        or metadata["version"] != operation.target.expected_version
                        or metadata.get("destroyed") is True or bool(metadata.get("deletion_time"))
                        or not isinstance(values, dict) or not isinstance(values.get("value"), str)
                        or values.get("_lightnow_operation_id") != str(operation.operation_id)):
                    raise RuntimeSecretError("Vault readback is unconfirmed")
                digest = hashlib.sha256(values["value"].encode("utf-8")).hexdigest()
                if operation.write is not None and not hmac.compare_digest(
                    digest, hashlib.sha256(operation.write.value.get_secret_value().encode("utf-8")).hexdigest(),
                ):
                    raise RuntimeSecretError("Vault readback is unconfirmed")
                if not self._current(operation):
                    raise RuntimeSecretError("Vault operation claim expired")
                return {**base, "status": "succeeded", "kv": {
                    "version": operation.target.expected_version,
                    "operation_marker": str(operation.operation_id), "value_sha256": digest,
                }}
        except _VaultRefusal as exc:
            return {**base, "status": "failed", "error_code": exc.code}
        except (httpx.HTTPError, RuntimeSecretError, UnicodeError):
            return {**base, "status": "outcome_unknown", "error_code": "VAULT_IO_UNCONFIRMED"}


class OAuthOperationExecutor(SecretOperationExecutor):
    """One native refresh at most; both pinned roles must be confirmed together."""

    async def execute(self, operation: OAuthOperation) -> dict[str, Any]:
        base = {"claim_id": str(operation.claim.id), "claim_revision": operation.claim.revision}
        if not self._current(operation):
            return {**base, "status": "outcome_unknown", "error_code": "VAULT_IO_UNCONFIRMED"}
        remaining = (operation.claim.expires_at - datetime.now(UTC)).total_seconds()
        try:
            async with asyncio.timeout(remaining):
                return await self._execute_oauth(operation, base)
        except TimeoutError:
            return {**base, "status": "outcome_unknown", "error_code": "VAULT_IO_UNCONFIRMED"}

    async def _read(self, client: httpx.AsyncClient, address: str, headers: dict[str, str],
                    path: str, version: int, marker: str) -> str:
        data = self._data(await self.resolver.vault_request(
            client, "GET", f"{address.rstrip('/')}/v1/{path}", headers=headers, params={"version": version},
        ), writing=False)
        metadata, values = data.get("metadata"), data.get("data")
        if (not isinstance(metadata, dict) or type(metadata.get("version")) is not int
                or metadata["version"] != version or metadata.get("destroyed") is True
                or bool(metadata.get("deletion_time")) or not isinstance(values, dict)
                or not isinstance(values.get("value"), str) or values.get("_lightnow_operation_id") != marker):
            raise RuntimeSecretError("OAuth KV readback is unconfirmed")
        return values["value"]

    @staticmethod
    def _grant(value: str, operation: OAuthOperation) -> dict[str, Any]:
        if len(value.encode()) > 8192:
            raise RuntimeSecretError("OAuth grant exceeds its size limit")
        try:
            grant = json.loads(value)
            metadata = OAuthMetadata.model_validate(grant["metadata"])
        except (ValueError, TypeError, KeyError):
            raise RuntimeSecretError("OAuth grant is malformed") from None
        if (not isinstance(grant.get("options"), dict)
                or grant.get("issuer") != operation.metadata.issuer
                or metadata.issuer != operation.metadata.issuer or metadata.endpoint != operation.metadata.endpoint
                or "refreshToken" not in grant
                or grant["refreshToken"] is not None and (not isinstance(grant["refreshToken"], str) or not grant["refreshToken"])):
            raise RuntimeSecretError("OAuth grant binding is unconfirmed")
        if (metadata.hasRefreshToken is not None
                and metadata.hasRefreshToken != isinstance(grant["refreshToken"], str)):
            raise RuntimeSecretError("OAuth grant binding is unconfirmed")
        return grant

    async def _refresh(self, grant: dict[str, Any], operation: OAuthOperation) -> dict[str, str]:
        options = grant["options"]
        if not isinstance(grant["refreshToken"], str) or not grant["refreshToken"]:
            raise _VaultRefusal("OAUTH_RECONNECT_REQUIRED")
        endpoint = options.get("urlAccessToken")
        if not isinstance(endpoint, str):
            raise _VaultRefusal("OAUTH_RECONNECT_REQUIRED")
        parsed = urlsplit(endpoint)
        client_id, client_secret = options.get("clientId"), options.get("clientSecret", "")
        method = options.get("tokenAuthMethod", "client_secret_post" if client_secret else "none")
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment
                or not isinstance(client_id, str) or not client_id or not isinstance(client_secret, str)
                or method not in ("none", "client_secret_basic", "client_secret_post")):
            raise _VaultRefusal("OAUTH_RECONNECT_REQUIRED")
        async with BoundedOAuth2Client(client_id=client_id, client_secret=client_secret,
                                      token_endpoint_auth_method=method, follow_redirects=False, timeout=15) as oauth:
            # League's existing renewal passes the MCP resource, without inventing
            # new scopes or registration. Authlib carries a missing refresh token.
            redirect = options.get("redirectUri")
            if redirect is not None and not isinstance(redirect, str):
                raise _VaultRefusal("OAUTH_RECONNECT_REQUIRED")
            token = await oauth.refresh_token(endpoint, refresh_token=grant["refreshToken"],
                                              resource=operation.metadata.endpoint,
                                              **({"redirect_uri": redirect} if redirect else {}))
        access, refresh = token.get("access_token"), token.get("refresh_token", grant["refreshToken"])
        expires = token.get("expires_at")
        if (not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh
                or str(token.get("token_type", "Bearer")).lower() != "bearer"
                or expires is not None and (type(expires) is not int or expires < 0)):
            raise _VaultRefusal("OAUTH_RECONNECT_REQUIRED")
        metadata = {**grant["metadata"], "expiresAt": expires, "obtainedAt": int(datetime.now(UTC).timestamp()),
                    "reconnect": False}
        next_grant = {"options": options, "issuer": grant["issuer"], "refreshToken": refresh, "metadata": metadata}
        values = {"access": "Bearer " + access, "grant": json.dumps(next_grant, separators=(",", ":"))}
        if any(len(value.encode()) > 8192 for value in values.values()):
            raise _VaultRefusal("OAUTH_RECONNECT_REQUIRED")
        return values

    async def _execute_oauth(self, operation: OAuthOperation, base: dict[str, Any]) -> dict[str, Any]:
        try:
            client, address, headers = await self.resolver.vault_connection(
                str(operation.provider.id), operation.provider.config.model_dump(), require_local=True,
            )
        except (RuntimeSecretError, OSError):
            return {**base, "status": "failed", "error_code": "PROVIDER_UNAVAILABLE"}
        try:
            async with client:
                if not self._current(operation):
                    raise RuntimeSecretError("OAuth claim expired")
                values = ({"access": operation.write.access.get_secret_value(),
                           "grant": operation.write.grant.get_secret_value()} if operation.write is not None else None)
                if operation.claim.action == "oauth.refresh":
                    reference = operation.grant_reference
                    old = await self._read(client, address, headers, reference.path, reference.version,
                                           str(reference.operation_id))
                    values = await self._refresh(self._grant(old, operation), operation)
                read_values, attestation = {}, {}
                for role in ("access", "grant"):
                    target = getattr(operation.targets, role)
                    if not self._current(operation):
                        raise RuntimeSecretError("OAuth claim expired")
                    url = f"{address.rstrip('/')}/v1/{target.path}"
                    if values is not None:
                        written = self._data(await self.resolver.vault_request(client, "POST", url, headers=headers,
                            json={"options": {"cas": target.cas}, "data": {"value": values[role],
                                  "_lightnow_operation_id": str(operation.operation_id)}}), writing=True)
                        if type(written.get("version")) is not int or written["version"] != target.expected_version:
                            raise RuntimeSecretError("OAuth write version is unconfirmed")
                    value = await self._read(client, address, headers, target.path, target.expected_version,
                                             str(operation.operation_id))
                    if values is not None and not hmac.compare_digest(value.encode(), values[role].encode()):
                        raise RuntimeSecretError("OAuth value is unconfirmed")
                    read_values[role] = value
                    attestation[role] = {"version": target.expected_version,
                                         "operation_marker": str(operation.operation_id),
                                         "value_sha256": hashlib.sha256(value.encode()).hexdigest()}
                grant = self._grant(read_values["grant"], operation)
                if not read_values["access"].startswith("Bearer ") or not self._current(operation):
                    raise RuntimeSecretError("OAuth pair is unconfirmed")
                return {**base, "status": "succeeded", "oauth": {
                    **attestation, "expires_at": grant["metadata"]["expiresAt"],
                }}
        except _VaultRefusal as exc:
            return {**base, "status": "failed", "error_code": exc.code}
        except OAuthError as exc:
            code = "OAUTH_PROVIDER_TEMPORARY" if exc.error in ("server_error", "temporarily_unavailable") else "OAUTH_RECONNECT_REQUIRED"
            return {**base, "status": "failed", "error_code": code}
        except httpx2.HTTPStatusError as exc:
            # Authlib raises a native HTTP error before decoding 5xx OAuth errors.
            # Only an explicit standard refusal is definitive; other IO stays unknown.
            try:
                error = exc.response.json().get("error")
            except (ValueError, AttributeError):
                error = None
            if error in ("server_error", "temporarily_unavailable"):
                return {**base, "status": "failed", "error_code": "OAUTH_PROVIDER_TEMPORARY"}
            return {**base, "status": "outcome_unknown", "error_code": "VAULT_IO_UNCONFIRMED"}
        except (httpx.HTTPError, httpx2.HTTPError, RuntimeSecretError, UnicodeError, ValueError):
            return {**base, "status": "outcome_unknown", "error_code": "VAULT_IO_UNCONFIRMED"}
