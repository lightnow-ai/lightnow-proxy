from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
from time import monotonic, time
from typing import Any, Mapping
from urllib.parse import quote, unquote

from mcp.types import CallToolResult, ReadResourceResult, Resource, ResourceTemplate, TextContent, Tool

from lightnow_proxy import __version__
from lightnow_proxy.config import ProfileConfig, ProxyConfig, UpstreamConfig
from lightnow_proxy.registry import RegistryApiClient
from lightnow_proxy.request_context import get_current_principal
from lightnow_proxy.upstream import UpstreamMCPClient

logger = logging.getLogger(__name__)


class ToolRoutingError(Exception):
    pass


def unwrap_error(exc: BaseException) -> BaseException:
    """Return the first leaf exception so reports show the actual cause
    instead of "ExceptionGroup: unhandled errors in a TaskGroup"."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def error_type_name(exc: BaseException) -> str:
    return type(unwrap_error(exc)).__name__


def error_message(exc: BaseException) -> str:
    leaf = unwrap_error(exc)
    return (str(leaf) or type(leaf).__name__)[:500]


@dataclass(frozen=True)
class RoutedTool:
    upstream_name: str
    original_name: str


@dataclass(frozen=True)
class RoutedResource:
    upstream_name: str
    original_uri: str


@dataclass(frozen=True)
class ProfileUpstream:
    config: UpstreamConfig
    server_name: str | None = None


@dataclass
class ClientRuntimeContext:
    name: str | None = None
    version: str | None = None
    capability_keys: list[str] | None = None


@dataclass(frozen=True)
class UpstreamHealth:
    name: str
    transport: str
    server_name: str | None
    status: str
    tool_count: int = 0
    duration_ms: int | None = None
    error_type: str | None = None
    error_message: str | None = None


class ToolRouter:
    def __init__(self, config: ProxyConfig, upstream_client: UpstreamMCPClient | None = None):
        self.config = config
        self.upstream_client = upstream_client or UpstreamMCPClient()
        self.registry_client = RegistryApiClient(config.registry_api) if config.registry_api and config.registry_api.enabled else None
        self.client_context = ClientRuntimeContext()

    def update_client_context(
        self,
        *,
        name: str | None = None,
        version: str | None = None,
        capability_keys: list[str] | None = None,
    ) -> None:
        if name:
            self.client_context.name = name
        if version:
            self.client_context.version = version
        if capability_keys is not None:
            self.client_context.capability_keys = capability_keys

    async def list_tools(self, profile_name: str) -> list[Tool]:
        started = monotonic()
        try:
            cached_tools = self._read_tool_cache(profile_name, fresh_only=True)
            if cached_tools is not None:
                await self._emit_runtime_event(
                    {
                        "profile": profile_name,
                        "event_type": "list_tools",
                        "status": "success",
                        "duration_ms": self._duration_ms(started),
                    }
                )
                return cached_tools
            upstreams = await self._profile_upstream_details(profile_name)
            tools: list[Tool] = []
            failures: list[tuple[str, Exception]] = []
            results = await asyncio.gather(
                *[
                    self._list_upstream_tools(upstream_name, upstream.config)
                    for upstream_name, upstream in upstreams.items()
                ]
            )
            for upstream_name, upstream_tools, exc in results:
                if exc is not None:
                    failures.append((upstream_name, exc))
                    logger.warning("failed to list tools for upstream %s: %s", upstream_name, unwrap_error(exc))
                    continue
                for tool in upstream_tools:
                    tools.append(prefix_tool(upstream_name, tool))
            await self._emit_runtime_event(
                {
                    "profile": profile_name,
                    "event_type": "list_tools",
                    "status": "error" if failures and not tools else "success",
                    "duration_ms": self._duration_ms(started),
                    "error_type": "UpstreamListToolsError" if failures and not tools else None,
                    "error_message": self._list_tools_error_message(failures) if failures and not tools else None,
                }
            )
            if tools:
                self._write_tool_cache(profile_name, tools)
            return tools
        except Exception as exc:
            cached_tools = self._read_tool_cache(profile_name, fresh_only=False)
            if cached_tools is not None:
                logger.warning("using stale tools cache for profile %s after list_tools failure: %s", profile_name, exc)
                await self._emit_runtime_event(
                    {
                        "profile": profile_name,
                        "event_type": "list_tools",
                        "status": "success",
                        "duration_ms": self._duration_ms(started),
                    }
                )
                return cached_tools
            await self._emit_runtime_event(
                {
                    "profile": profile_name,
                    "event_type": "list_tools",
                    "status": "error",
                    "duration_ms": self._duration_ms(started),
                    "error_type": error_type_name(exc),
                    "error_message": error_message(exc),
                }
            )
            raise

    async def _list_upstream_tools(self, upstream_name: str, config: UpstreamConfig) -> tuple[str, list[Tool], Exception | None]:
        try:
            return upstream_name, await self.upstream_client.list_tools(config), None
        except Exception as exc:
            return upstream_name, [], exc

    async def profile_upstream_health(self, profile_name: str) -> list[UpstreamHealth]:
        upstreams = await self._profile_upstream_details(profile_name)
        results = await asyncio.gather(
            *[
                self._measure_upstream_health(upstream_name, upstream)
                for upstream_name, upstream in upstreams.items()
            ]
        )
        return sorted(results, key=lambda item: item.name)

    async def _measure_upstream_health(self, upstream_name: str, upstream: ProfileUpstream) -> UpstreamHealth:
        started = monotonic()
        _, tools, exc = await self._list_upstream_tools(upstream_name, upstream.config)
        duration_ms = self._duration_ms(started)
        if exc is not None:
            return UpstreamHealth(
                name=upstream_name,
                transport=upstream.config.transport,
                server_name=upstream.server_name,
                status="error",
                duration_ms=duration_ms,
                error_type=error_type_name(exc),
                error_message=error_message(exc),
            )
        return UpstreamHealth(
            name=upstream_name,
            transport=upstream.config.transport,
            server_name=upstream.server_name,
            status="healthy",
            tool_count=len(tools),
            duration_ms=duration_ms,
        )

    def _tool_cache_path(self, profile_name: str) -> Path | None:
        local_proxy = self.config.local_proxy
        if not local_proxy.enabled or not local_proxy.tool_cache_enabled:
            return None
        if local_proxy.tool_cache_path:
            return Path(local_proxy.tool_cache_path).expanduser()
        if not local_proxy.client_name or local_proxy.client_name == "local-proxy":
            return None
        client = _safe_cache_part(local_proxy.client_name or "client")
        profile = _safe_cache_part(profile_name)
        return Path.home() / ".lightnow" / "lightnow-proxy" / "cache" / f"{client}-{profile}-tools.json"

    def _read_tool_cache(self, profile_name: str, *, fresh_only: bool) -> list[Tool] | None:
        path = self._tool_cache_path(profile_name)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
            generated_at = float(payload["generated_at"])
            ttl = max(0, int(self.config.local_proxy.tool_cache_ttl_seconds))
            if fresh_only and time() - generated_at > ttl:
                return None
            tools = payload["tools"]
            if not isinstance(tools, list):
                return None
            return [Tool.model_validate(tool) for tool in tools]
        except Exception as exc:
            logger.warning("failed to read tools cache %s: %s", path, exc)
            return None

    def _write_tool_cache(self, profile_name: str, tools: list[Tool]) -> None:
        path = self._tool_cache_path(profile_name)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "generated_at": time(),
                "tools": [tool.model_dump(exclude_none=True) for tool in tools],
            }
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, separators=(",", ":")))
            temp_path.replace(path)
        except Exception as exc:
            logger.warning("failed to write tools cache %s: %s", path, exc)

    async def call_tool(
        self,
        profile_name: str,
        proxied_tool_name: str,
        arguments: dict[str, Any] | None,
        request_meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        started = monotonic()
        routed: RoutedTool | None = None
        upstream: ProfileUpstream | None = None
        try:
            routed, upstream = await self._call_target(profile_name, proxied_tool_name)
            if upstream is None:
                result = CallToolResult(
                    content=[
                        TextContent(type="text", text=f"Tool {proxied_tool_name!r} is not available in this profile.")
                    ],
                    isError=True,
                )
                await self._emit_call_event(
                    profile_name,
                    proxied_tool_name,
                    routed,
                    None,
                    started,
                    result,
                    arguments,
                    request_meta,
                )
                return result
            result = await self.upstream_client.call_tool(upstream.config, routed.original_name, arguments or {})
            await self._emit_call_event(
                profile_name,
                proxied_tool_name,
                routed,
                upstream,
                started,
                result,
                arguments,
                request_meta,
            )
            return result
        except Exception as exc:
            await self._emit_runtime_event(
                {
                    "profile": profile_name,
                    "event_type": "call_tool",
                    "status": "error",
                    "server_alias": routed.upstream_name if routed else None,
                    "server_name": upstream.server_name if upstream else None,
                    "tool_name": proxied_tool_name,
                    "original_tool_name": routed.original_name if routed else None,
                    "duration_ms": self._duration_ms(started),
                    "request_bytes": tool_arguments_size(arguments),
                    **runtime_request_metadata(arguments, request_meta),
                    "error_type": error_type_name(exc),
                    "error_message": error_message(exc),
                }
            )
            raise

    async def list_resources(self, profile_name: str) -> list[Resource]:
        started = monotonic()
        try:
            upstreams = await self._profile_upstream_details(profile_name)
            resources: list[Resource] = []
            failures: list[tuple[str, Exception]] = []
            for upstream_name, upstream in upstreams.items():
                try:
                    upstream_resources = await self.upstream_client.list_resources(upstream.config)
                except Exception as exc:
                    failures.append((upstream_name, exc))
                    logger.warning("failed to list resources for upstream %s: %s", upstream_name, unwrap_error(exc))
                    continue
                for resource in upstream_resources:
                    resources.append(prefix_resource(upstream_name, resource))
            await self._emit_runtime_event(
                {
                    "profile": profile_name,
                    "event_type": "list_resources",
                    "status": "error" if failures and not resources else "success",
                    "duration_ms": self._duration_ms(started),
                    "response_items_count": len(resources),
                    "error_type": "UpstreamListResourcesError" if failures and not resources else None,
                    "error_message": self._list_tools_error_message(failures) if failures and not resources else None,
                }
            )
            return resources
        except Exception as exc:
            await self._emit_runtime_event(
                {
                    "profile": profile_name,
                    "event_type": "list_resources",
                    "status": "error",
                    "duration_ms": self._duration_ms(started),
                    "error_type": error_type_name(exc),
                    "error_message": error_message(exc),
                }
            )
            raise

    async def list_resource_templates(self, profile_name: str) -> list[ResourceTemplate]:
        started = monotonic()
        try:
            upstreams = await self._profile_upstream_details(profile_name)
            templates: list[ResourceTemplate] = []
            failures: list[tuple[str, Exception]] = []
            for upstream_name, upstream in upstreams.items():
                try:
                    upstream_templates = await self.upstream_client.list_resource_templates(upstream.config)
                except Exception as exc:
                    failures.append((upstream_name, exc))
                    logger.warning("failed to list resource templates for upstream %s: %s", upstream_name, unwrap_error(exc))
                    continue
                for template in upstream_templates:
                    templates.append(prefix_resource_template(upstream_name, template))
            await self._emit_runtime_event(
                {
                    "profile": profile_name,
                    "event_type": "list_resource_templates",
                    "status": "error" if failures and not templates else "success",
                    "duration_ms": self._duration_ms(started),
                    "response_items_count": len(templates),
                    "error_type": "UpstreamListResourceTemplatesError" if failures and not templates else None,
                    "error_message": self._list_tools_error_message(failures) if failures and not templates else None,
                }
            )
            return templates
        except Exception as exc:
            await self._emit_runtime_event(
                {
                    "profile": profile_name,
                    "event_type": "list_resource_templates",
                    "status": "error",
                    "duration_ms": self._duration_ms(started),
                    "error_type": error_type_name(exc),
                    "error_message": error_message(exc),
                }
            )
            raise

    async def read_resource(self, profile_name: str, proxied_uri: str) -> ReadResourceResult:
        started = monotonic()
        routed: RoutedResource | None = None
        upstream: ProfileUpstream | None = None
        try:
            routed = route_resource(proxied_uri)
            upstreams = await self._profile_upstream_details(profile_name)
            upstream = upstreams.get(routed.upstream_name)
            if upstream is None:
                raise ToolRoutingError(f"Resource upstream {routed.upstream_name!r} is not available in this profile.")
            result = await self.upstream_client.read_resource(upstream.config, routed.original_uri)
            await self._emit_runtime_event(
                {
                    "profile": profile_name,
                    "event_type": "read_resource",
                    "status": "success",
                    "server_alias": routed.upstream_name,
                    "server_name": upstream.server_name,
                    "resource_uri": proxied_uri,
                    "duration_ms": self._duration_ms(started),
                    "response_items_count": len(result.contents),
                    "response_bytes": resource_result_size(result),
                    "response_content_type": first_resource_mime_type(result),
                    "upstream_transport": upstream.config.transport,
                }
            )
            return result
        except Exception as exc:
            await self._emit_runtime_event(
                {
                    "profile": profile_name,
                    "event_type": "read_resource",
                    "status": "error",
                    "server_alias": routed.upstream_name if routed else None,
                    "server_name": upstream.server_name if upstream else None,
                    "resource_uri": proxied_uri,
                    "duration_ms": self._duration_ms(started),
                    "error_type": error_type_name(exc),
                    "error_message": error_message(exc),
                    "upstream_transport": upstream.config.transport if upstream else None,
                }
            )
            raise

    def route_tool(
        self,
        proxied_tool_name: str,
        upstreams: Mapping[str, object] | None = None,
    ) -> RoutedTool:
        upstream_names = upstreams.keys() if upstreams is not None else self.config.upstreams.keys()
        for upstream_name in sorted(upstream_names, key=len, reverse=True):
            prefix = f"{upstream_name}__"
            if proxied_tool_name.startswith(prefix):
                original = proxied_tool_name[len(prefix) :]
                if not original:
                    break
                return RoutedTool(upstream_name=upstream_name, original_name=original)
        raise ToolRoutingError(f"unknown proxied tool: {proxied_tool_name}")

    def _profile(self, profile_name: str) -> ProfileConfig:
        try:
            return self.config.profiles[profile_name]
        except KeyError as exc:
            raise ToolRoutingError(f"unknown profile: {profile_name}") from exc

    async def _call_target(self, profile_name: str, proxied_tool_name: str) -> tuple[RoutedTool, ProfileUpstream | None]:
        profile = self._profile(profile_name)
        upstreams = {name: ProfileUpstream(config=self.config.upstreams[name]) for name in profile.upstreams}

        if profile.runtime_upstreams:
            if self.registry_client is None:
                raise ToolRoutingError("profile uses runtime_upstreams but registry_api is not enabled")
            principal = get_current_principal()
            for runtime_upstream in profile.runtime_upstreams:
                resolved = await self.registry_client.resolve_upstream(runtime_upstream, principal)
                upstreams[resolved.name] = ProfileUpstream(config=resolved.config, server_name=runtime_upstream.server)

        lightnow_names: list[str] = []
        if self.config.local_proxy.enabled and self.config.local_proxy.sync_from_lightnow:
            if self.registry_client is None:
                raise ToolRoutingError("local proxy sync requires registry_api to be enabled")
            lightnow_names = await self.registry_client.fetch_profile_upstream_names(profile_name)

        routable_upstreams: dict[str, object] = {**self.config.upstreams}
        routable_upstreams.update({name: None for name in lightnow_names})
        routed = self.route_tool(proxied_tool_name, routable_upstreams)
        upstream = upstreams.get(routed.upstream_name)
        if upstream is not None:
            return routed, upstream

        if routed.upstream_name in lightnow_names:
            resolved = await self.registry_client.fetch_profile_upstream(
                profile_name,
                routed.upstream_name,
                get_current_principal(),
            )
            return routed, ProfileUpstream(config=resolved.config, server_name=resolved.server_name)

        return routed, None

    async def _profile_upstreams(self, profile_name: str) -> dict[str, UpstreamConfig]:
        return {name: upstream.config for name, upstream in (await self._profile_upstream_details(profile_name)).items()}

    async def _profile_upstream_details(self, profile_name: str) -> dict[str, ProfileUpstream]:
        profile = self._profile(profile_name)
        upstreams = {name: ProfileUpstream(config=self.config.upstreams[name]) for name in profile.upstreams}

        if profile.runtime_upstreams:
            if self.registry_client is None:
                raise ToolRoutingError("profile uses runtime_upstreams but registry_api is not enabled")
            principal = get_current_principal()
            for runtime_upstream in profile.runtime_upstreams:
                resolved = await self.registry_client.resolve_upstream(runtime_upstream, principal)
                upstreams[resolved.name] = ProfileUpstream(config=resolved.config, server_name=runtime_upstream.server)

        if self.config.local_proxy.enabled and self.config.local_proxy.sync_from_lightnow:
            if self.registry_client is None:
                raise ToolRoutingError("local proxy sync requires registry_api to be enabled")
            principal = get_current_principal()
            for resolved in await self.registry_client.fetch_profile_upstreams(profile_name, principal):
                upstreams[resolved.name] = ProfileUpstream(
                    config=resolved.config,
                    server_name=resolved.server_name,
                )

        return upstreams

    async def _emit_call_event(
        self,
        profile_name: str,
        proxied_tool_name: str,
        routed: RoutedTool,
        upstream: ProfileUpstream | None,
        started: float,
        result: CallToolResult,
        arguments: dict[str, Any] | None,
        request_meta: dict[str, Any] | None,
    ) -> None:
        await self._emit_runtime_event(
            {
                "profile": profile_name,
                "event_type": "call_tool",
                "status": "error" if result.isError else "success",
                "server_alias": routed.upstream_name,
                "server_name": upstream.server_name if upstream else None,
                "tool_name": proxied_tool_name,
                "original_tool_name": routed.original_name,
                "duration_ms": self._duration_ms(started),
                "request_bytes": tool_arguments_size(arguments),
                **runtime_request_metadata(arguments, request_meta),
                "upstream_transport": upstream.config.transport if upstream else None,
                "response_items_count": len(result.content),
                "response_bytes": call_tool_result_size(result),
                "response_content_type": first_call_tool_content_type(result),
            }
        )

    async def _emit_runtime_event(self, payload: dict[str, Any]) -> None:
        if self.registry_client is None or not self.config.local_proxy.telemetry_enabled:
            return
        client_name = self.client_context.name or self.config.local_proxy.client_name
        client_version = self.client_context.version or self.config.local_proxy.client_version
        event = {
            key: value
            for key, value in {
                **payload,
                "client_name": client_name,
                "mcp_client_name": client_name,
                "mcp_client_version": client_version,
                "runner_name": self.config.local_proxy.runner_name,
                "runner_version": __version__,
                "client_transport": self.config.local_proxy.client_transport,
                "local_proxy_policy_mode": self.config.local_proxy.policy_mode,
                "local_proxy_allow_unmanaged_client_servers": self.config.local_proxy.allow_unmanaged_client_servers,
                "mcp_method": mcp_method_for_event(str(payload.get("event_type", ""))),
            }.items()
            if value is not None
        }
        try:
            await self.registry_client.post_runtime_event(event)
        except Exception as exc:  # pragma: no cover - defensive logging only
            logger.warning("failed to post LightNow MCP runtime event: %s", exc)

    async def emit_lifecycle_event(self, event_type: str, status: str = "success") -> None:
        await self._emit_runtime_event(
            {
                "profile": self.config.local_proxy.profile,
                "event_type": event_type,
                "status": status,
            }
        )

    async def emit_health_event(self, report: dict[str, Any]) -> None:
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        health_status = report.get("status") if isinstance(report.get("status"), str) else "failed"
        await self._emit_runtime_event(
            {
                "profile": self.config.local_proxy.profile,
                "event_type": "proxy_health",
                "status": "success" if health_status == "healthy" else "error",
                "proxy_health_status": health_status if health_status in {"healthy", "degraded", "failed"} else "failed",
                "proxy_upstreams": int(summary.get("upstreams") or 0),
                "proxy_healthy_upstreams": int(summary.get("healthy_upstreams") or 0),
                "proxy_failed_upstreams": int(summary.get("failed_upstreams") or 0),
                "proxy_tool_count": int(summary.get("tools") or 0),
            }
        )

    def _duration_ms(self, started: float) -> int:
        return max(0, int((monotonic() - started) * 1000))

    def _list_tools_error_message(self, failures: list[tuple[str, Exception]]) -> str:
        return "; ".join(f"{name}: {error_type_name(exc)}" for name, exc in failures)[:500]


def prefix_tool(upstream_name: str, tool: Tool) -> Tool:
    data = tool.model_dump(exclude_none=True)
    data = copy.deepcopy(data)
    data["name"] = f"{upstream_name}__{tool.name}"
    description = data.get("description") or ""
    data["description"] = f"[{upstream_name}] {description}".strip()
    meta = dict(data.get("meta") or data.get("_meta") or {})
    meta["lightnow_proxy_upstream"] = upstream_name
    meta["lightnow_proxy_original_name"] = tool.name
    data["_meta"] = meta
    return Tool.model_validate(data)


def prefix_resource(upstream_name: str, resource: Resource) -> Resource:
    original_uri = str(resource.uri)
    data = resource.model_dump(exclude_none=True, by_alias=True)
    data = copy.deepcopy(data)
    data["uri"] = proxied_resource_uri(upstream_name, original_uri)
    data["name"] = f"{upstream_name} / {resource.name}"
    meta = dict(data.get("_meta") or {})
    meta["lightnow_proxy_upstream"] = upstream_name
    meta["lightnow_proxy_original_uri"] = original_uri
    data["_meta"] = meta
    return Resource.model_validate(data)


def prefix_resource_template(upstream_name: str, template: ResourceTemplate) -> ResourceTemplate:
    data = template.model_dump(exclude_none=True, by_alias=True)
    data = copy.deepcopy(data)
    data["uriTemplate"] = f"lightnow://{upstream_name}/{{encoded_uri}}"
    data["name"] = f"{upstream_name} / {template.name}"
    meta = dict(data.get("_meta") or {})
    meta["lightnow_proxy_upstream"] = upstream_name
    meta["lightnow_proxy_original_uri_template"] = template.uriTemplate
    data["_meta"] = meta
    return ResourceTemplate.model_validate(data)


def proxied_resource_uri(upstream_name: str, original_uri: str) -> str:
    return f"lightnow://{upstream_name}/{quote(original_uri, safe='')}"


def route_resource(proxied_uri: str) -> RoutedResource:
    prefix = "lightnow://"
    if not proxied_uri.startswith(prefix):
        raise ToolRoutingError(f"unknown proxied resource URI: {proxied_uri}")
    remainder = proxied_uri[len(prefix) :]
    upstream_name, separator, encoded_uri = remainder.partition("/")
    if not upstream_name or separator != "/" or not encoded_uri:
        raise ToolRoutingError(f"invalid proxied resource URI: {proxied_uri}")
    return RoutedResource(upstream_name=upstream_name, original_uri=unquote(encoded_uri))


def _safe_cache_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "default"


def call_tool_result_size(result: CallToolResult) -> int:
    return len(result.model_dump_json(exclude_none=True).encode("utf-8"))


def tool_arguments_size(arguments: dict[str, Any] | None) -> int:
    return len(json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def runtime_request_metadata(arguments: dict[str, Any] | None, request_meta: dict[str, Any] | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "argument_keys": sorted((arguments or {}).keys()),
    }
    if not isinstance(request_meta, dict):
        return metadata

    metadata["request_meta_keys"] = sorted(request_meta.keys())
    thread_id = request_meta.get("threadId")
    if isinstance(thread_id, str):
        metadata["mcp_thread_id"] = thread_id
        metadata["client_context_thread_id"] = thread_id

    metadata.update(codex_client_context(request_meta))
    antigravity_context = antigravity_client_context(request_meta)
    if "mcp_thread_id" in metadata:
        antigravity_context.pop("mcp_thread_id", None)
    metadata.update(antigravity_context)

    return metadata


def codex_client_context(request_meta: Mapping[str, Any]) -> dict[str, Any]:
    codex = request_meta.get("x-codex-turn-metadata")
    if not isinstance(codex, dict):
        return {}

    metadata: dict[str, Any] = {"client_context_metadata": {"adapter": "codex"}}
    for source_key, target_key in {
        "session_id": "codex_session_id",
        "thread_id": "codex_thread_id",
        "turn_id": "codex_turn_id",
        "model": "codex_model",
        "reasoning_effort": "codex_reasoning_effort",
        "sandbox": "codex_sandbox",
    }.items():
        value = codex.get(source_key)
        if isinstance(value, str) and value:
            metadata[target_key] = value

    for source_key, target_key in {
        "session_id": "client_context_session_id",
        "thread_id": "client_context_thread_id",
        "turn_id": "client_context_turn_id",
        "model": "client_context_model",
        "reasoning_effort": "client_context_reasoning",
        "sandbox": "client_context_sandbox",
    }.items():
        value = codex.get(source_key)
        if isinstance(value, str) and value:
            metadata[target_key] = value

    return metadata


def antigravity_client_context(request_meta: Mapping[str, Any]) -> dict[str, Any]:
    conversation_id = request_meta.get("antigravity.google/conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return {}

    metadata: dict[str, Any] = {
        "mcp_thread_id": conversation_id,
        "client_context_thread_id": conversation_id,
        "client_context_metadata": {
            "adapter": "antigravity",
        },
    }
    if "progressToken" in request_meta:
        metadata["client_context_metadata"]["progress_reported"] = True
    return metadata


def resource_result_size(result: ReadResourceResult) -> int:
    return len(result.model_dump_json(exclude_none=True).encode("utf-8"))


def first_call_tool_content_type(result: CallToolResult) -> str | None:
    if not result.content:
        return None
    return getattr(result.content[0], "type", None)


def first_resource_mime_type(result: ReadResourceResult) -> str | None:
    if not result.contents:
        return None
    return getattr(result.contents[0], "mimeType", None)


def mcp_method_for_event(event_type: str) -> str | None:
    return {
        "list_tools": "tools/list",
        "call_tool": "tools/call",
        "list_resources": "resources/list",
        "read_resource": "resources/read",
        "list_resource_templates": "resources/templates/list",
    }.get(event_type)
