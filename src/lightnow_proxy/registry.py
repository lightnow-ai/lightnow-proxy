from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import quote

import httpx
import jwt
from pydantic import ValidationError

from lightnow_proxy.auth import Principal
from lightnow_proxy.config import RegistryApiConfig, RuntimeUpstreamConfig, UpstreamConfig
from lightnow_proxy.runtime_secrets import RuntimeSecretResolver


class RegistryApiError(Exception):
    pass


@dataclass(frozen=True)
class CliSession:
    access_token: str
    refresh_token: str | None
    issuer: str
    client_id: str
    tenant_id: str | None
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "CliSession":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RegistryApiError("LightNow CLI session not found. Run `lightnow login` first.") from exc
        except json.JSONDecodeError as exc:
            raise RegistryApiError(f"LightNow CLI config is invalid JSON: {path}") from exc

        if not isinstance(raw, dict):
            raise RegistryApiError("LightNow CLI config must be a JSON object")
        access_token = raw.get("access_token")
        issuer = raw.get("issuer") or "https://auth.lightnow.ai/realms/lightnow"
        client_id = raw.get("client_id") or "lightnow-cli"
        refresh_token = raw.get("refresh_token")
        context_type = raw.get("context_type")
        context_tenant = raw.get("context_tenant")
        if not isinstance(access_token, str) or access_token == "":
            raise RegistryApiError("LightNow CLI session has no access token. Run `lightnow login` first.")
        if not isinstance(issuer, str) or issuer == "":
            raise RegistryApiError("LightNow CLI config has no issuer")
        if not isinstance(client_id, str) or client_id == "":
            raise RegistryApiError("LightNow CLI config has no client_id")
        tenant_id = context_tenant if context_type == "tenant" and isinstance(context_tenant, str) else None
        return cls(
            access_token=access_token,
            refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
            issuer=issuer,
            client_id=client_id,
            tenant_id=tenant_id,
            raw=raw,
        )

    def access_token_expired(self) -> bool:
        try:
            payload = jwt.decode(self.access_token, options={"verify_signature": False, "verify_exp": False})
        except jwt.PyJWTError as exc:
            raise RegistryApiError("LightNow CLI access token is invalid. Run `lightnow login` again.") from exc
        exp = payload.get("exp")
        if not isinstance(exp, int | float):
            return False
        return datetime.now(UTC).timestamp() >= float(exp) - 30

    def with_tokens(self, access_token: str, refresh_token: str | None) -> "CliSession":
        raw = dict(self.raw)
        raw["access_token"] = access_token
        if refresh_token is not None:
            raw["refresh_token"] = refresh_token
        return CliSession(
            access_token=access_token,
            refresh_token=refresh_token or self.refresh_token,
            issuer=self.issuer,
            client_id=self.client_id,
            tenant_id=self.tenant_id,
            raw=raw,
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        fd, raw_tmp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        tmp_path = Path(raw_tmp_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(self.raw, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            path.chmod(0o600)
        except Exception:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise


@dataclass(frozen=True)
class ResolvedRuntimeUpstream:
    name: str
    config: UpstreamConfig
    server_name: str | None = None


async def refresh_cli_session(
    session: CliSession,
    timeout_seconds: float,
    verify: str | bool = True,
) -> CliSession:
    if not session.refresh_token:
        raise RegistryApiError("LightNow CLI session expired. Run `lightnow login` again.")

    token_url = f"{session.issuer.rstrip('/')}/protocol/openid-connect/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": session.client_id,
        "refresh_token": session.refresh_token,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), verify=verify) as client:
        response = await client.post(token_url, data=data)
    if response.status_code >= 400:
        raise RegistryApiError("LightNow CLI session refresh failed. Run `lightnow login` again.")
    payload = response.json()
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or access_token == "":
        raise RegistryApiError("LightNow token endpoint did not return an access token")
    refresh_token = payload.get("refresh_token")
    return session.with_tokens(
        access_token,
        refresh_token if isinstance(refresh_token, str) and refresh_token else None,
    )


class RegistryApiClient:
    def __init__(self, config: RegistryApiConfig, runtime_secret_resolver: RuntimeSecretResolver | None = None):
        self.config = config
        self.runtime_secret_resolver = runtime_secret_resolver
        self._access_token: str | None = None
        self._tenant_id: str | None = None

    def _verify(self) -> str | bool:
        return self.config.resolved_ca_file() or True

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds),
            verify=self._verify(),
        )

    async def fetch_profile_runtime_upstreams(self, profile_name: str) -> list[RuntimeUpstreamConfig]:
        headers = await self._authorization_headers()
        url = self._api_url(f"/integrations/profiles/{profile_name}/servers")
        async with self._http_client() as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        servers = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(servers, list):
            raise RegistryApiError("Registry profile servers response must include a servers array")

        upstreams: list[RuntimeUpstreamConfig] = []
        for item in servers:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "linked":
                alias = item.get("alias") or item.get("server_name") or "unknown"
                raise RegistryApiError(f"Registry profile server {alias!r} is not linked")
            alias = item.get("alias")
            server_name = item.get("server_name")
            version = item.get("version")
            if not isinstance(alias, str) or alias == "":
                raise RegistryApiError("Registry profile server is missing alias")
            if not isinstance(server_name, str) or server_name == "":
                raise RegistryApiError(f"Registry profile server {alias!r} is missing server_name")
            if not isinstance(version, str) or version == "":
                raise RegistryApiError(f"Registry profile server {alias!r} is missing version")
            upstreams.append(
                RuntimeUpstreamConfig(
                    name=alias,
                    server=server_name,
                    version=version,
                    runtime_profile=profile_name,
                )
            )
        return upstreams

    async def fetch_profile_upstreams(
        self,
        profile_name: str,
        principal: Principal | None,
    ) -> list[ResolvedRuntimeUpstream]:
        headers = await self._authorization_headers()
        url = self._api_url(f"/integrations/profiles/{profile_name}/servers")
        params = {"include": "secrets"} if self.config.include_secrets else None
        async with self._http_client() as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
        servers = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(servers, list):
            raise RegistryApiError("Registry profile servers response must include a servers array")

        upstreams: list[ResolvedRuntimeUpstream] = []
        for item in servers:
            if not isinstance(item, dict):
                continue
            alias = item.get("alias")
            server_name = item.get("server_name")
            status = item.get("status")
            if not isinstance(alias, str) or alias == "":
                raise RegistryApiError("Registry profile server is missing alias")
            if not isinstance(server_name, str) or server_name == "":
                raise RegistryApiError(f"Registry profile server {alias!r} is missing server_name")

            if status == "linked":
                version = item.get("version")
                if not isinstance(version, str) or version == "":
                    raise RegistryApiError(f"Registry profile server {alias!r} is missing version")
                resolved = await self.resolve_upstream(
                    RuntimeUpstreamConfig(
                        name=alias,
                        server=server_name,
                        version=version,
                        runtime_profile=profile_name,
                    ),
                    principal,
                )
                upstreams.append(
                    ResolvedRuntimeUpstream(
                        name=resolved.name,
                        config=resolved.config,
                        server_name=server_name,
                    )
                )
                continue

            if status == "custom":
                client_config = item.get("client_config")
                if not isinstance(client_config, dict):
                    raise RegistryApiError(f"Custom profile server {alias!r} is missing client_config")
                upstreams.append(
                    ResolvedRuntimeUpstream(
                        name=alias,
                        config=upstream_config_from_client_config(client_config),
                        server_name=server_name,
                    )
                )
                continue

            raise RegistryApiError(f"Registry profile server {alias!r} is not runnable")
        return upstreams

    async def fetch_profile_upstream_names(self, profile_name: str) -> list[str]:
        payload = await self._fetch_profile_servers(profile_name, include_secrets=False)
        servers = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(servers, list):
            raise RegistryApiError("Registry profile servers response must include a servers array")

        names: list[str] = []
        for item in servers:
            if not isinstance(item, dict):
                continue
            alias = item.get("alias")
            if isinstance(alias, str) and alias:
                names.append(alias)
        return names

    async def fetch_profile_upstream(
        self,
        profile_name: str,
        alias: str,
        principal: Principal | None,
    ) -> ResolvedRuntimeUpstream:
        payload = await self._fetch_profile_servers(profile_name, include_secrets=self.config.include_secrets)
        servers = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(servers, list):
            raise RegistryApiError("Registry profile servers response must include a servers array")

        for item in servers:
            if isinstance(item, dict) and item.get("alias") == alias:
                return await self._profile_server_to_upstream(item, profile_name, principal)

        raise RegistryApiError(f"Registry profile server {alias!r} is not available")

    async def post_runtime_event(self, payload: dict[str, Any]) -> None:
        headers = await self._authorization_headers()
        url = self._api_url("/integrations/runtime-events")
        async with self._http_client() as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()

    async def post_device_heartbeat(
        self,
        installation_id: str,
        client_instance_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Post one observed device heartbeat using the current CLI scope."""
        headers = await self._authorization_headers()
        url = self._api_url(f"/integrations/devices/{installation_id}/clients/{client_instance_id}/heartbeat")
        async with self._http_client() as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict):
            raise RegistryApiError("Registry device heartbeat response must be a JSON object")
        return result

    async def _fetch_profile_servers(self, profile_name: str, include_secrets: bool) -> dict[str, Any]:
        headers = await self._authorization_headers()
        url = self._api_url(f"/integrations/profiles/{profile_name}/servers")
        params = {"include": "secrets"} if include_secrets else None
        async with self._http_client() as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RegistryApiError("Registry profile servers response must be a JSON object")
        return payload

    async def _profile_server_to_upstream(
        self,
        item: dict[str, Any],
        profile_name: str,
        principal: Principal | None,
    ) -> ResolvedRuntimeUpstream:
        alias = item.get("alias")
        server_name = item.get("server_name")
        status = item.get("status")
        if not isinstance(alias, str) or alias == "":
            raise RegistryApiError("Registry profile server is missing alias")
        if not isinstance(server_name, str) or server_name == "":
            raise RegistryApiError(f"Registry profile server {alias!r} is missing server_name")

        if status == "linked":
            version = item.get("version")
            if not isinstance(version, str) or version == "":
                raise RegistryApiError(f"Registry profile server {alias!r} is missing version")
            resolved = await self.resolve_upstream(
                RuntimeUpstreamConfig(
                    name=alias,
                    server=server_name,
                    version=version,
                    runtime_profile=profile_name,
                ),
                principal,
            )
            return ResolvedRuntimeUpstream(
                name=resolved.name,
                config=resolved.config,
                server_name=server_name,
            )

        if status == "custom":
            client_config = item.get("client_config")
            if not isinstance(client_config, dict):
                raise RegistryApiError(f"Custom profile server {alias!r} is missing client_config")
            return ResolvedRuntimeUpstream(
                name=alias,
                config=upstream_config_from_client_config(client_config),
                server_name=server_name,
            )

        raise RegistryApiError(f"Registry profile server {alias!r} is not runnable")

    async def resolve_upstream(
        self,
        upstream: RuntimeUpstreamConfig,
        principal: Principal | None,
    ) -> ResolvedRuntimeUpstream:
        context = await self.fetch_runtime_context(upstream, principal)
        if self.runtime_secret_resolver is not None:
            context = await self.runtime_secret_resolver.resolve_context(context)
        try:
            return ResolvedRuntimeUpstream(
                name=upstream.name,
                config=upstream_config_from_runtime_context(context, upstream),
                server_name=upstream.server,
            )
        except (KeyError, TypeError, ValidationError, ValueError) as exc:
            raise RegistryApiError(
                f"Registry runtime context for {upstream.server}:{upstream.version} is not usable"
            ) from exc

    async def fetch_runtime_context(
        self,
        upstream: RuntimeUpstreamConfig,
        principal: Principal | None,
    ) -> dict[str, Any]:
        headers = await self._authorization_headers()
        params = {
            "profile": upstream.runtime_profile,
        }
        if self.config.include_secrets:
            params["include"] = "secrets"
        if upstream.scope_type is not None:
            params["scopeType"] = upstream.scope_type
        elif not self.config.use_cli_session:
            params["scopeType"] = self.config.default_scope_type
        if upstream.transport:
            params["transport"] = _registry_transport(upstream.transport)
        params["consumer"] = "local-runner"
        scope_id = self._scope_id(upstream, principal)
        if scope_id:
            params["scopeId"] = scope_id

        server_name = quote(upstream.server, safe="")
        version = quote(upstream.version, safe="")
        url = self._api_url(f"/servers/{server_name}/versions/{version}/context")
        async with self._http_client() as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RegistryApiError("Registry runtime context response must be a JSON object")
        return payload

    async def _authorization_headers(self) -> dict[str, str]:
        if self.config.use_cli_session:
            session = await self._cli_session()
            headers = {"Authorization": f"Bearer {session.access_token}"}
            tenant_id = self.config.cli_tenant_id or session.tenant_id
            if tenant_id:
                headers["X-Tenant"] = tenant_id
            return headers

        token = await self._client_credentials_token()
        if token is None:
            return {}
        return {"Authorization": f"Bearer {token}"}

    async def _cli_session(self) -> "CliSession":
        path = Path(os.path.expanduser(self.config.cli_config_path))
        session = CliSession.load(path)
        if session.access_token_expired():
            session = await refresh_cli_session(session, self.config.timeout_seconds, verify=self._verify())
            session.save(path)
        self._tenant_id = session.tenant_id
        return session

    async def _client_credentials_token(self) -> str | None:
        if self._access_token is not None:
            return self._access_token
        if self.config.token_url is None or not self.config.client_id or not self.config.client_secret:
            return None

        data = {
            "grant_type": "client_credentials",
            "client_id": self.config.client_id,
            "client_secret": self.config.resolved_client_secret() or "",
        }
        if self.config.audience:
            data["audience"] = self.config.audience

        async with self._http_client() as client:
            response = await client.post(str(self.config.token_url), data=data)
            response.raise_for_status()
            payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or token == "":
            raise RegistryApiError("Registry token endpoint did not return an access_token")
        self._access_token = token
        return token

    def _scope_id(self, upstream: RuntimeUpstreamConfig, principal: Principal | None) -> str | None:
        if upstream.scope_id:
            return upstream.scope_id

        scope_type = upstream.scope_type or self.config.default_scope_type
        if scope_type == "system" or principal is None:
            if upstream.scope_id:
                return upstream.scope_id
            return None
        claim = self.config.user_scope_claim if scope_type == "user" else self.config.tenant_scope_claim
        value = principal.claims.get(claim)
        return value if isinstance(value, str) and value != "" else None

    def _api_url(self, path: str) -> str:
        base_url = str(self.config.base_url).rstrip("/")
        if base_url.endswith("/v0.1"):
            return f"{base_url}{path}"
        return f"{base_url}/v0.1{path}"


def upstream_config_from_runtime_context(
    context: dict[str, Any],
    upstream: RuntimeUpstreamConfig,
) -> UpstreamConfig:
    probe_request = context["probe_request"]
    transport = probe_request["transport"]
    timeout_seconds = upstream.timeout_seconds or 30.0

    if transport == "http":
        http_config = probe_request["http"]
        return UpstreamConfig(
            transport="streamable-http",
            url=http_config["url"],
            headers=_string_map(http_config.get("headers")),
            timeout_seconds=timeout_seconds,
        )

    if transport == "stdio":
        stdio_config = probe_request["stdio"]
        return UpstreamConfig(
            transport="stdio",
            command=stdio_config["cmd"],
            args=_string_list(stdio_config.get("args")),
            env=_string_map(stdio_config.get("env")),
            timeout_seconds=timeout_seconds,
        )

    if transport == "sse":
        raise RegistryApiError("SSE runtime contexts are not supported by this proxy yet")

    raise RegistryApiError(f"unsupported Registry runtime transport: {transport}")


def upstream_config_from_client_config(client_config: dict[str, Any]) -> UpstreamConfig:
    transport = client_config.get("transport") or "stdio"
    timeout_seconds = _timeout_seconds(client_config)

    if transport in {"http", "streamable-http"}:
        url = client_config.get("url")
        if not isinstance(url, str) or url == "":
            raise RegistryApiError("HTTP custom server config is missing url")
        return UpstreamConfig(
            transport="streamable-http",
            url=url,
            headers=_string_map(client_config.get("headers")),
            timeout_seconds=timeout_seconds,
        )

    if transport == "stdio":
        command = client_config.get("command")
        if not isinstance(command, str) or command == "":
            raise RegistryApiError("stdio custom server config is missing command")
        cwd = client_config.get("cwd") or client_config.get("working_directory")
        return UpstreamConfig(
            transport="stdio",
            command=command,
            args=_string_list(client_config.get("args")),
            env=_string_map(client_config.get("env")),
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            timeout_seconds=timeout_seconds,
        )

    raise RegistryApiError(f"unsupported custom server transport: {transport}")


def _timeout_seconds(client_config: dict[str, Any]) -> float:
    for key in ("tool_timeout_sec", "startup_timeout_sec"):
        value = client_config.get(key)
        if isinstance(value, int | float) and value > 0:
            return float(value)
    value = client_config.get("startup_timeout_ms")
    if isinstance(value, int | float) and value > 0:
        return float(value) / 1000
    return 30.0


def _registry_transport(transport: str) -> str:
    if transport == "streamable-http":
        return "streamable-http"
    return transport


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
