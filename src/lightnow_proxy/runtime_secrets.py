from __future__ import annotations

import asyncio
from typing import Any

import httpx

from lightnow_proxy.config import RuntimeSecretProviderConfig, RuntimeSecretsConfig


class RuntimeSecretError(Exception):
    """A runtime-only secret reference could not be resolved safely."""


class RuntimeSecretResolver:
    MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(self, config: RuntimeSecretsConfig):
        self.config = config

    async def resolve_context(self, context: dict[str, Any]) -> dict[str, Any]:
        bindings = context.get("external_secret_bindings")
        if bindings is None:
            return context
        if not isinstance(bindings, list):
            raise RuntimeSecretError("Registry runtime context contains invalid external secret bindings")

        for binding in bindings:
            if not isinstance(binding, dict):
                raise RuntimeSecretError("Registry runtime context contains an invalid external secret binding")
            self._assert_target_matches(context, binding)
            value = await self._resolve_binding(binding)
            self._inject(context, binding, value)

        return context

    async def _resolve_binding(self, binding: dict[str, Any]) -> str:
        provider = binding.get("provider")
        locator = binding.get("locator")
        if not isinstance(provider, dict) or not isinstance(locator, dict):
            raise RuntimeSecretError("External secret binding is missing provider metadata or locator")

        provider_id = provider.get("id")
        provider_type = provider.get("provider_type")
        resolution_mode = provider.get("resolution_mode")
        path = locator.get("path")
        field = locator.get("field")
        version = locator.get("version")
        operation_id = locator.get("operation_id")
        if not all(isinstance(item, str) and item for item in (provider_id, path, field)):
            raise RuntimeSecretError("External secret binding is incomplete")
        if "version" in locator and (type(version) is not int or version < 1):
            raise RuntimeSecretError("External secret binding has an invalid pinned version")
        if "operation_id" in locator and (not isinstance(operation_id, str) or not operation_id or version is None):
            raise RuntimeSecretError("External secret binding has an invalid pinned operation")
        if resolution_mode != "runtime":
            raise RuntimeSecretError("External secret binding is not marked for runtime resolution")
        if provider_type != "vault_kv_v2":
            raise RuntimeSecretError(f"Unsupported runtime secret provider type: {provider_type}")

        provider_config = provider.get("config") if isinstance(provider.get("config"), dict) else {}
        client, address, headers = await self.vault_connection(provider_id, provider_config)
        url = f"{str(address).rstrip('/')}/v1/{str(path).lstrip('/')}"
        try:
            async with client:
                response = await self.vault_request(client, "GET", url, headers=headers, params={"version": version} if version else None)
        except httpx.HTTPError as exc:
            raise RuntimeSecretError(f"Runtime Vault provider {provider_id} is not reachable") from exc

        if response.is_redirect:
            raise RuntimeSecretError(f"Runtime Vault provider {provider_id} returned a redirect")
        if response.status_code >= 400:
            raise RuntimeSecretError(f"Runtime Vault provider {provider_id} returned HTTP {response.status_code}")
        if len(response.content) > self.MAX_RESPONSE_BYTES:
            raise RuntimeSecretError(f"Runtime Vault provider {provider_id} returned an oversized response")

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeSecretError(f"Runtime Vault provider {provider_id} returned invalid JSON") from exc
        payload_data = payload.get("data") if isinstance(payload, dict) else None
        data = payload_data if isinstance(payload_data, dict) else {}
        secret_data = data.get("data") if isinstance(data.get("data"), dict) else data
        if version is not None:
            metadata = data.get("metadata")
            returned_version = metadata.get("version") if isinstance(metadata, dict) else None
            if (type(returned_version) is not int or returned_version != version
                    or metadata.get("destroyed") is True or bool(metadata.get("deletion_time"))):
                raise RuntimeSecretError(f"Runtime Vault provider {provider_id} did not confirm the pinned version")
        if operation_id is not None and secret_data.get("_lightnow_operation_id") != operation_id:
            raise RuntimeSecretError(f"Runtime Vault provider {provider_id} did not confirm the pinned operation")
        value = secret_data.get(field) if isinstance(secret_data, dict) else None
        if not isinstance(value, str | int | float | bool):
            raise RuntimeSecretError(f"Runtime Vault provider {provider_id} did not return the configured scalar field")
        return str(value)

    async def vault_connection(
        self, provider_id: str, provider_config: dict[str, Any], *, require_local: bool = False,
    ) -> tuple[httpx.AsyncClient, str, dict[str, str]]:
        """Reuse the existing TLS/bootstrap path; writes require explicit local enrollment."""
        local = self.config.providers.get(provider_id)
        if require_local and (local is None or local.auth_mode == "keyring" and not local.address_override):
            raise RuntimeSecretError("Vault writes require an explicitly mapped local provider and address")
        local = local or RuntimeSecretProviderConfig()
        namespace = provider_config.get("namespace")
        headers: dict[str, str] = {}
        if isinstance(namespace, str) and namespace:
            headers["X-Vault-Namespace"] = namespace

        if local.auth_mode == "vault-proxy":
            address = local.vault_proxy_url
        else:
            address = local.address_override or provider_config.get("address")
            if not isinstance(address, str) or not address:
                raise RuntimeSecretError(f"Vault address is missing for runtime provider {provider_id}")
            token = await self._keyring_token(local, provider_id)
            headers["X-Vault-Token"] = token

        verify: str | bool = local.resolved_ca_file() or True
        return httpx.AsyncClient(timeout=httpx.Timeout(local.timeout_seconds), verify=verify, follow_redirects=False), str(address), headers

    @staticmethod
    async def vault_request(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Bound decompressed IO before buffering Vault's untrusted response."""
        async with client.stream(method, url, **kwargs) as response:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes(chunk_size=8192):
                size += len(chunk)
                if size > RuntimeSecretResolver.MAX_RESPONSE_BYTES:
                    raise RuntimeSecretError("Runtime Vault provider returned an oversized response")
                chunks.append(chunk)
            headers = response.headers.copy()
            # aiter_bytes has already decoded Content-Encoding; preserve status
            # and metadata without asking the new response to decode it again.
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx.Response(response.status_code, headers=headers, content=b"".join(chunks), request=response.request)

    async def _keyring_token(self, config: RuntimeSecretProviderConfig, provider_id: str) -> str:
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeSecretError(
                "OS-keyring support is not installed. Install lightnow-proxy[keyring] or use Vault Proxy."
            ) from exc

        token = await asyncio.to_thread(keyring.get_password, config.keyring_service, provider_id)
        if not isinstance(token, str) or not token:
            raise RuntimeSecretError(f"OS keyring has no Vault token for runtime provider {provider_id}")
        return token

    def _inject(self, context: dict[str, Any], binding: dict[str, Any], value: str) -> None:
        target = binding.get("target")
        probe = context.get("probe_request")
        if not isinstance(target, dict) or not isinstance(probe, dict):
            raise RuntimeSecretError("External secret binding has no valid injection target")
        target_type = target.get("type")
        name = target.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeSecretError("External secret binding target has no name")

        if target_type == "env" and probe.get("transport") == "stdio":
            stdio = probe.setdefault("stdio", {})
            if not isinstance(stdio, dict):
                raise RuntimeSecretError("Runtime context contains invalid stdio configuration")
            env = stdio.setdefault("env", {})
            if not isinstance(env, dict):
                raise RuntimeSecretError("Runtime context contains invalid stdio environment")
            env[name] = value
            return

        if target_type == "header" and probe.get("transport") in {"http", "sse"}:
            transport = str(probe.get("transport"))
            remote = probe.setdefault(transport, {})
            if not isinstance(remote, dict):
                raise RuntimeSecretError("Runtime context contains invalid remote configuration")
            headers = remote.setdefault("headers", {})
            if not isinstance(headers, dict):
                raise RuntimeSecretError("Runtime context contains invalid remote headers")
            headers[name] = value
            return

        raise RuntimeSecretError("External secret binding target does not match the selected transport")

    def _assert_target_matches(self, context: dict[str, Any], binding: dict[str, Any]) -> None:
        target = binding.get("target")
        probe = context.get("probe_request")
        if not isinstance(target, dict) or not isinstance(probe, dict):
            raise RuntimeSecretError("External secret binding has no valid injection target")
        target_type = target.get("type")
        transport = probe.get("transport")
        if target_type == "env" and transport == "stdio":
            return
        if target_type == "header" and transport in {"http", "sse"}:
            return
        raise RuntimeSecretError("External secret binding target does not match the selected transport")
