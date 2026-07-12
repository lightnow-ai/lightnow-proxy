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
        if not all(isinstance(item, str) and item for item in (provider_id, path, field)):
            raise RuntimeSecretError("External secret binding is incomplete")
        if resolution_mode != "runtime":
            raise RuntimeSecretError("External secret binding is not marked for runtime resolution")
        if provider_type != "vault_kv_v2":
            raise RuntimeSecretError(f"Unsupported runtime secret provider type: {provider_type}")

        local = self.config.providers.get(provider_id, RuntimeSecretProviderConfig())
        provider_config = provider.get("config") if isinstance(provider.get("config"), dict) else {}
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

        url = f"{str(address).rstrip('/')}/v1/{str(path).lstrip('/')}"
        verify: str | bool = local.resolved_ca_file() or True
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(local.timeout_seconds),
                verify=verify,
                follow_redirects=False,
            ) as client:
                response = await client.get(url, headers=headers)
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
        value = secret_data.get(field) if isinstance(secret_data, dict) else None
        if not isinstance(value, str | int | float | bool):
            raise RuntimeSecretError(f"Runtime Vault provider {provider_id} did not return the configured scalar field")
        return str(value)

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
