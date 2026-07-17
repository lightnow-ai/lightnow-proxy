from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
import yaml

from lightnow_proxy import __version__


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str = "http://localhost:8080"


class LocalProxyConfig(BaseModel):
    enabled: bool = False
    connection_id: UUID | None = None
    connection_alias: str = "lightnow"
    profile: str = "default"
    scope_type: Literal["personal", "tenant"] = "personal"
    scope_id: str | None = None
    account_label: str | None = None
    path: str = "/mcp"
    sync_from_lightnow: bool = False
    client_name: str = "local-proxy"
    client_version: str | None = None
    runner_name: str = "lightnow-local-proxy"
    runner_version: str = __version__
    client_transport: Literal["stdio", "streamable-http"] | None = None
    telemetry_enabled: bool = True
    policy_mode: Literal["observe", "enforce"] = "observe"
    allow_unmanaged_client_servers: bool = True
    tool_cache_enabled: bool = True
    tool_cache_ttl_seconds: int = 300
    tool_cache_path: str | None = None
    device_installation_id: UUID | None = None
    client_instance_id: UUID | None = None
    device_hostname: str | None = None
    device_platform: Literal["macos", "windows", "linux", "unknown"] = "unknown"

    @field_validator("path")
    @classmethod
    def path_must_be_absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("local proxy path must start with /")
        return value.rstrip("/") or "/"


class AuthConfig(BaseModel):
    enabled: bool = True
    issuer: str
    audience: str | list[str] | None = None
    groups_claim: str = "groups"
    jwks_cache_seconds: int = 300
    dev_bearer_tokens: dict[str, dict[str, Any]] = Field(default_factory=dict)


class RuntimeUpstreamConfig(BaseModel):
    name: str
    server: str
    version: str
    runtime_profile: str = "default"
    instance_alias: str | None = None
    transport: Literal["streamable-http", "stdio", "sse"] | None = None
    scope_type: Literal["system", "tenant", "user"] | None = None
    scope_id: str | None = None
    timeout_seconds: float | None = None


class ProfileConfig(BaseModel):
    required_groups: list[str] = Field(default_factory=list)
    upstreams: list[str] = Field(default_factory=list)
    runtime_upstreams: list[RuntimeUpstreamConfig] = Field(default_factory=list)

    @field_validator("required_groups", "upstreams")
    @classmethod
    def not_empty_items(cls, value: list[str]) -> list[str]:
        return [item for item in value if item]


class UpstreamConfig(BaseModel):
    transport: Literal["streamable-http", "stdio"] = "streamable-http"
    url: HttpUrl | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 30.0
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    @model_validator(mode="after")
    def transport_settings_are_complete(self) -> "UpstreamConfig":
        if self.transport == "streamable-http" and self.url is None:
            raise ValueError("streamable-http upstreams require url")
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio upstreams require command")
        return self

    def resolved_headers(self) -> dict[str, str]:
        return {key: expand_env(value) for key, value in self.headers.items()}

    def resolved_env(self) -> dict[str, str]:
        return {key: expand_env(value) for key, value in self.env.items()}


class RegistryApiConfig(BaseModel):
    enabled: bool = False
    base_url: HttpUrl
    ca_file: str | None = None
    token_url: HttpUrl | None = None
    client_id: str | None = None
    client_secret: str | None = None
    audience: str | None = None
    include_secrets: bool = True
    default_scope_type: Literal["system", "tenant", "user"] = "system"
    timeout_seconds: float = 20.0
    user_scope_claim: str = "sub"
    tenant_scope_claim: str = "lightnow_tenant_id"
    use_cli_session: bool = False
    cli_config_path: str = "~/.lightnow/config.json"
    cli_session_path: str | None = None
    expected_issuer: str | None = None
    expected_subject: str | None = None
    cli_tenant_id: str | None = None

    @model_validator(mode="after")
    def named_cli_session_must_have_identity_guardrails(self) -> "RegistryApiConfig":
        if self.use_cli_session and self.cli_session_path:
            if not self.expected_issuer or not self.expected_subject:
                raise ValueError("cli_session_path requires expected_issuer and expected_subject")
        return self

    def resolved_client_secret(self) -> str | None:
        if self.client_secret is None:
            return None
        return expand_env(self.client_secret)

    def resolved_ca_file(self) -> str | None:
        if self.ca_file is None:
            return None
        return os.path.expanduser(expand_env(self.ca_file))


class RuntimeSecretProviderConfig(BaseModel):
    auth_mode: Literal["vault-proxy", "keyring"] = "vault-proxy"
    vault_proxy_url: str = "http://127.0.0.1:8200"
    address_override: str | None = None
    ca_file: str | None = None
    keyring_service: str = "lightnow-proxy-vault"
    timeout_seconds: float = 10.0

    @field_validator("vault_proxy_url")
    @classmethod
    def vault_proxy_must_be_local(cls, value: str) -> str:
        parsed = urlparse(value)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or host is None:
            raise ValueError("vault_proxy_url must be an HTTP(S) origin")
        is_loopback = host == "localhost"
        try:
            is_loopback = is_loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if not is_loopback:
            raise ValueError("vault_proxy_url must target a loopback address")
        return value.rstrip("/")

    @field_validator("address_override")
    @classmethod
    def direct_vault_address_must_use_https(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise ValueError("address_override must be an HTTPS origin")
        return value.rstrip("/")

    def resolved_ca_file(self) -> str | None:
        if self.ca_file is None:
            return None
        return os.path.expanduser(expand_env(self.ca_file))


class RuntimeSecretsConfig(BaseModel):
    providers: dict[str, RuntimeSecretProviderConfig] = Field(default_factory=dict)


class ProxyConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    local_proxy: LocalProxyConfig = Field(default_factory=LocalProxyConfig)
    auth: AuthConfig
    registry_api: RegistryApiConfig | None = None
    runtime_secrets: RuntimeSecretsConfig = Field(default_factory=RuntimeSecretsConfig)
    profiles: dict[str, ProfileConfig]
    upstreams: dict[str, UpstreamConfig]

    @model_validator(mode="after")
    def bind_legacy_cli_sessions_to_configured_issuer(self) -> "ProxyConfig":
        """Keep legacy configs readable while preventing cross-environment tokens."""
        if (
            self.registry_api
            and self.registry_api.enabled
            and self.registry_api.use_cli_session
            and self.registry_api.expected_issuer is None
        ):
            self.registry_api.expected_issuer = self.auth.issuer
        return self

    @field_validator("profiles")
    @classmethod
    def profiles_must_exist(cls, value: dict[str, ProfileConfig]) -> dict[str, ProfileConfig]:
        if not value:
            raise ValueError("at least one profile must be configured")
        return value

    def validate_references(self) -> None:
        unknown: dict[str, list[str]] = {}
        for profile_name, profile in self.profiles.items():
            missing = [name for name in profile.upstreams if name not in self.upstreams]
            if missing:
                unknown[profile_name] = missing
        if unknown:
            raise ValueError(f"profiles reference unknown upstreams: {unknown}")


def expand_env(value: str) -> str:
    expanded = os.path.expandvars(value)
    if "$" in expanded:
        raise ValueError(f"unresolved environment variable in value for {value!r}")
    return expanded


def load_config(path: str | os.PathLike[str]) -> ProxyConfig:
    config_path = Path(path).expanduser()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    config = ProxyConfig.model_validate(raw)
    config.validate_references()
    return config
