"""Execute one Registry-issued private KV2 claim, without retries or stored values."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import hmac
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, StrictInt, model_validator

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
        prefix = f"{self.provider.config.mount}/data/{self.provider.config.base_path}/"
        suffix = self.target.path.removeprefix(prefix)
        parts = suffix.split("/")
        # Both path IDs are opaque server-owned UUIDs. The final ID may be the
        # original operation when CAS replaces an already pinned reference.
        if not self.target.path.startswith(prefix) or len(parts) != 2:
            raise ValueError("Invalid secret operation target")
        for part in parts:
            if str(UUID(part)) != part or UUID(part).int == 0:
                raise ValueError("Invalid secret operation target")
        return self


class _VaultRefusal(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


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
