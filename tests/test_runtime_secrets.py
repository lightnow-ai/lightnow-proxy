from __future__ import annotations

import pytest
import respx
from httpx import Response

from lightnow_proxy.config import RuntimeSecretProviderConfig, RuntimeSecretsConfig
from lightnow_proxy.runtime_secrets import RuntimeSecretError, RuntimeSecretResolver


def runtime_context(*, target_type: str = "env", target_name: str = "API_TOKEN") -> dict:
    transport = "stdio" if target_type == "env" else "http"
    probe = (
        {"transport": "stdio", "stdio": {"cmd": "npx", "args": []}}
        if transport == "stdio"
        else {"transport": "http", "http": {"url": "https://mcp.example.test", "headers": {}}}
    )
    return {
        "probe_request": probe,
        "external_secret_bindings": [
            {
                "provider": {
                    "id": "provider-uuid",
                    "provider_type": "vault_kv_v2",
                    "resolution_mode": "runtime",
                    "config": {
                        "address": "https://vault.customer.internal",
                        "namespace": "customer",
                    },
                },
                "locator": {"path": "secret/data/lightnow/github", "field": "token"},
                "target": {"type": target_type, "name": target_name},
            }
        ],
    }


@pytest.mark.asyncio
async def test_resolves_vault_proxy_binding_into_stdio_environment_without_sending_a_token() -> None:
    resolver = RuntimeSecretResolver(RuntimeSecretsConfig())

    with respx.mock(assert_all_called=True) as router:
        route = router.get("http://127.0.0.1:8200/v1/secret/data/lightnow/github").respond(
            200,
            json={"data": {"data": {"token": "runtime-value"}}},
        )
        resolved = await resolver.resolve_context(runtime_context())

    assert resolved["probe_request"]["stdio"]["env"]["API_TOKEN"] == "runtime-value"
    assert "X-Vault-Token" not in route.calls.last.request.headers
    assert route.calls.last.request.headers["X-Vault-Namespace"] == "customer"


@pytest.mark.asyncio
async def test_resolves_runtime_binding_into_http_header() -> None:
    resolver = RuntimeSecretResolver(RuntimeSecretsConfig())

    with respx.mock(assert_all_called=True) as router:
        router.get("http://127.0.0.1:8200/v1/secret/data/lightnow/github").respond(
            200,
            json={"data": {"data": {"token": "Bearer runtime-value"}}},
        )
        resolved = await resolver.resolve_context(runtime_context(target_type="header", target_name="Authorization"))

    assert resolved["probe_request"]["http"]["headers"]["Authorization"] == "Bearer runtime-value"


@pytest.mark.asyncio
async def test_uses_configured_vault_proxy_and_never_falls_back_to_provider_address() -> None:
    resolver = RuntimeSecretResolver(
        RuntimeSecretsConfig(
            providers={
                "provider-uuid": RuntimeSecretProviderConfig(
                    auth_mode="vault-proxy",
                    vault_proxy_url="http://127.0.0.1:18200",
                )
            }
        )
    )

    with respx.mock(assert_all_called=True) as router:
        router.get("http://127.0.0.1:18200/v1/secret/data/lightnow/github").respond(503)
        with pytest.raises(RuntimeSecretError, match="HTTP 503"):
            await resolver.resolve_context(runtime_context())


@pytest.mark.asyncio
async def test_rejects_redirects_and_oversized_responses() -> None:
    resolver = RuntimeSecretResolver(RuntimeSecretsConfig())

    with respx.mock(assert_all_called=True) as router:
        router.get("http://127.0.0.1:8200/v1/secret/data/lightnow/github").respond(
            302, headers={"Location": "http://169.254.169.254/"}
        )
        with pytest.raises(RuntimeSecretError, match="redirect"):
            await resolver.resolve_context(runtime_context())

    with respx.mock(assert_all_called=True) as router:
        router.get("http://127.0.0.1:8200/v1/secret/data/lightnow/github").mock(
            return_value=Response(200, content=b"x" * (RuntimeSecretResolver.MAX_RESPONSE_BYTES + 1))
        )
        with pytest.raises(RuntimeSecretError, match="oversized"):
            await resolver.resolve_context(runtime_context())


@pytest.mark.asyncio
async def test_rejects_provider_and_target_contract_mismatches() -> None:
    context = runtime_context()
    context["external_secret_bindings"][0]["provider"]["provider_type"] = "aws_secrets_manager"

    with pytest.raises(RuntimeSecretError, match="Unsupported runtime secret provider"):
        await RuntimeSecretResolver(RuntimeSecretsConfig()).resolve_context(context)

    context = runtime_context(target_type="header")
    context["probe_request"] = {"transport": "stdio", "stdio": {"cmd": "npx"}}
    with pytest.raises(RuntimeSecretError, match="does not match"):
        await RuntimeSecretResolver(RuntimeSecretsConfig()).resolve_context(context)


def test_runtime_provider_config_restricts_proxy_to_loopback_and_direct_vault_to_https() -> None:
    with pytest.raises(ValueError, match="loopback"):
        RuntimeSecretProviderConfig(vault_proxy_url="http://vault.attacker.example:8200")

    with pytest.raises(ValueError, match="HTTPS"):
        RuntimeSecretProviderConfig(auth_mode="keyring", address_override="http://vault.internal")
