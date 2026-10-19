from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
from time import monotonic, time
from typing import Any, Mapping
from urllib.parse import quote, unquote
from uuid import UUID

import httpx

from mcp.types import CallToolResult, ReadResourceResult, Resource, ResourceTemplate, TextContent, Tool

from lightnow_proxy import __version__
from lightnow_proxy.config import ProfileConfig, ProxyConfig, UpstreamConfig
from lightnow_proxy.diagnostics import diagnostic_for_exception
from lightnow_proxy.managed_events import EvidenceStoreError, ManagedEventOutbox
from lightnow_proxy.registry import MANAGED_CAPABILITY, ManagedRuntime, ManagedToolDenied, RegistryApiClient, RegistryApiError, managed_tool_namespace
from lightnow_proxy.runtime_secrets import RuntimeSecretResolver
from lightnow_proxy.request_context import get_current_mcp_client_context, get_current_principal
from lightnow_proxy.upstream import ToolDiscoveryError, ToolInventory, UpstreamMCPClient
from lightnow_proxy.version_inventory import heartbeat_version_fields

logger = logging.getLogger(__name__)
DEVICE_HEARTBEAT_INTERVAL_SECONDS = 120
MANAGED_HEARTBEAT_INTERVAL_SECONDS = 15
REDACTED_ARGUMENT_VALUE = "[REDACTED]"
SENSITIVE_ARGUMENT_KEY_PARTS = {
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "token",
}


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


@dataclass(frozen=True, repr=False)
class ProfileUpstream:
    config: UpstreamConfig
    server_name: str | None = None
    managed: ManagedRuntime | None = None


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
    diagnostic_code: str | None = None
    diagnostic_kind: str | None = None
    diagnostic_summary: str | None = None
    remediation: str | None = None


class ToolRouter:
    def __init__(self, config: ProxyConfig, upstream_client: UpstreamMCPClient | None = None):
        self.config = config
        self.upstream_client = upstream_client or UpstreamMCPClient()
        self.registry_client = (
            RegistryApiClient(
                config.registry_api,
                RuntimeSecretResolver(config.runtime_secrets),
                report_receipt_observer=self._remember_reporter,
                runtime_files_connection_id=(
                    str(config.local_proxy.connection_id)
                    if config.local_proxy.connection_id
                    else config.local_proxy.connection_alias
                ),
            )
            if config.registry_api and config.registry_api.enabled
            else None
        )
        self.client_context = ClientRuntimeContext()
        self.device_heartbeat_last_attempt_at: datetime | None = None
        self.device_heartbeat_last_success_at: datetime | None = None
        self.device_heartbeat_last_error: str | None = None
        self._discovery_tasks: dict[str, asyncio.Task[None]] = {}
        self._managed_outbox: ManagedEventOutbox | None = None
        self._managed_report_error: str | None = None
        self._managed_upload_lock = asyncio.Lock()
        self._managed_applications: dict[str, tuple[ManagedRuntime, str]] = {}
        self._managed_application_error: str | None = None

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
                    self._list_upstream_tools(upstream_name, upstream)
                    for upstream_name, upstream in upstreams.items()
                ]
            )
            for upstream_name, upstream_tools, exc in results:
                if exc is not None:
                    failures.append((upstream_name, exc))
                    logger.warning("failed to list tools for upstream %s: %s", upstream_name, unwrap_error(exc))
                    continue
                for tool in upstream_tools:
                    tools.append(prefix_tool(upstream_name, tool, managed=upstreams[upstream_name].managed))
            tools.sort(key=lambda item: item.name)
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

    async def _list_upstream_tools(
        self, upstream_name: str, upstream: ProfileUpstream
    ) -> tuple[str, list[Tool], Exception | None]:
        try:
            if upstream.managed is None:
                return upstream_name, await self.upstream_client.list_tools(upstream.config), None
            if self.registry_client is None:
                raise ToolRoutingError("MANAGED_CONTROL_PLANE_REQUIRED")
            upstream.managed.assert_current()
            async with self.upstream_client.session(upstream.config) as client:
                inventory = await self.upstream_client.discover_tools(client)
                observations: list[str] = []
                allowed = await self.registry_client.authorize_inventory(upstream.managed, inventory, observed=observations.append)
            upstream.managed.assert_current()
            if observations:
                self._queue_managed_application(upstream.managed, observations[-1])
            return upstream_name, [tool for tool in inventory.tools if tool.name in allowed], None
        except Exception as exc:
            return upstream_name, [], exc

    async def profile_upstream_health(self, profile_name: str) -> list[UpstreamHealth]:
        upstreams = await self._profile_upstream_details(profile_name)
        results = await asyncio.gather(
            *[self._measure_upstream_health(upstream_name, upstream) for upstream_name, upstream in upstreams.items()]
        )
        return sorted(results, key=lambda item: item.name)

    async def _measure_upstream_health(self, upstream_name: str, upstream: ProfileUpstream) -> UpstreamHealth:
        started = monotonic()
        _, tools, exc = await self._list_upstream_tools(upstream_name, upstream)
        duration_ms = self._duration_ms(started)
        if exc is not None:
            leaf = unwrap_error(exc)
            diagnostic = diagnostic_for_exception(leaf)
            return UpstreamHealth(
                name=upstream_name,
                transport=upstream.config.transport,
                server_name=upstream.server_name,
                status="error",
                duration_ms=duration_ms,
                error_type=error_type_name(leaf),
                error_message=error_message(leaf),
                diagnostic_code=diagnostic.code,
                diagnostic_kind=diagnostic.kind,
                diagnostic_summary=diagnostic.summary,
                remediation=diagnostic.remediation,
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
        if (not local_proxy.enabled or not local_proxy.tool_cache_enabled
                or local_proxy.sync_from_lightnow):
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
        reserved = self.managed_events_enabled() and reserved_managed_namespace(proxied_tool_name.partition("__")[0])
        try:
            if reserved:
                # Commit an unattributed attempt before any control-plane IO.
                self._event_outbox().unattributed()
            try:
                routed, upstream = await self._call_target(profile_name, proxied_tool_name)
            except BaseException as resolution_error:
                await self._record_unresolved_managed_attempt(profile_name, proxied_tool_name, resolution_error, reserved=reserved)
                raise
            if upstream is None:
                await self._record_unresolved_managed_attempt(profile_name, proxied_tool_name, ToolRoutingError("MANAGED_RUNTIME_NOT_READY"), reserved=reserved)
                result = CallToolResult(
                    content=[
                        TextContent(type="text", text=f"Tool {proxied_tool_name!r} is not available in this profile.")
                    ],
                    is_error=True,
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
            if upstream.managed is None:
                result = await self.upstream_client.call_tool(upstream.config, routed.original_name, arguments or {})
            else:
                result = await self._call_managed_tool(upstream, routed.original_name, arguments or {}, reserved=reserved)
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
                    **runtime_request_metadata(
                        arguments,
                        request_meta,
                        capture_arguments=self.config.local_proxy.capture_tool_arguments and not ((upstream and upstream.managed) or reserved_managed_namespace(proxied_tool_name.partition("__")[0])),
                    ),
                    "error_type": error_type_name(exc),
                    "error_message": None if reserved_managed_namespace(proxied_tool_name.partition("__")[0]) else error_message(exc),
                }
            )
            leaf = unwrap_error(exc)
            if isinstance(leaf, EvidenceStoreError):
                raise ToolRoutingError("EVIDENCE_STORE_UNAVAILABLE") from None
            if isinstance(leaf, RegistryApiError) and str(leaf) in {
                "MANAGED_TOOL_DENIED", "MANAGED_AUTHORITY_EXPIRED", "MANAGED_CONTEXT_MISMATCH",
                "MANAGED_RUNTIME_NOT_READY", "INVALID_MANAGED_CONTEXT", "INVALID_TOOL_AUTHORIZATION", "MANAGED_ASSIGNMENT_UNAVAILABLE",
            }:
                # Expected managed decisions use the existing downstream MCP
                # tool-error path, including through native SDK task groups.
                raise ToolRoutingError(str(leaf)) from None
            raise

    def managed_events_enabled(self) -> bool:
        return bool(self.registry_client and self.config.local_proxy.enabled and self.config.local_proxy.sync_from_lightnow)

    def _event_outbox(self) -> ManagedEventOutbox:
        if self._managed_outbox is None:
            if self.config.registry_api is None:
                raise EvidenceStoreError()
            session_path = self.config.registry_api.cli_session_path or self.config.registry_api.cli_config_path
            local = self.config.local_proxy
            runtime_key = (f"{local.device_installation_id}:{local.client_instance_id}"
                           if local.device_installation_id and local.client_instance_id else
                           f"runtime:{self.registry_client.runtime_client_id}" if self.registry_client and self.registry_client.runtime_client_id else "unregistered")
            self._managed_outbox = ManagedEventOutbox(Path(session_path).expanduser().parent / "managed-runtime" / "events.sqlite3", runtime_key=runtime_key)
        return self._managed_outbox

    async def _remember_reporter(self, managed: ManagedRuntime) -> None:
        if self.registry_client is None:
            raise EvidenceStoreError()
        identity = await self.registry_client.managed_report_identity(refresh_tokens=False)
        self._event_outbox().remember(managed, identity)

    async def _record_unresolved_managed_attempt(self, profile: str, wire_name: str, error: BaseException, *, reserved: bool) -> None:
        namespace = wire_name.partition("__")[0]
        if not self.managed_events_enabled() or not reserved_managed_namespace(namespace) or self.registry_client is None:
            return
        outbox = self._event_outbox()
        try:
            identity = await self.registry_client.managed_report_identity(refresh_tokens=False)
        except RegistryApiError:
            return
        call = outbox.begin_cached(identity, profile, str(UUID(hex=namespace[3:])), reserved=reserved)
        if call is None:
            return
        try:
            leaf = unwrap_error(error)
            phase = "runtime_failed"
            code = "CONTROL_PLANE_UNAVAILABLE" if isinstance(leaf, httpx.HTTPError) else (
                "MANAGED_CONTEXT_MISMATCH" if str(leaf) == "MANAGED_CONTEXT_MISMATCH" else "MANAGED_RUNTIME_NOT_READY")
            if isinstance(leaf, RegistryApiError) and str(leaf) in {"MANAGED_AUTHORITY_EXPIRED", "MANAGED_ASSIGNMENT_UNAVAILABLE"}:
                phase, code = "denied", "ASSIGNMENT_UNAVAILABLE" if str(leaf) == "MANAGED_ASSIGNMENT_UNAVAILABLE" else "MANAGED_AUTHORITY_EXPIRED"
            if isinstance(leaf, httpx.HTTPStatusError):
                try:
                    body = leaf.response.json()
                except ValueError:
                    body = None
                detail = body.get("error") if isinstance(body, dict) else None
                if (leaf.response.status_code == 403 and isinstance(body, dict)
                        and (body.get("status") == 403 or isinstance(detail, dict) and detail.get("code") in {"FORBIDDEN", "RUNTIME_PROFILE_FORBIDDEN"})):
                    phase, code = "denied", "PROFILE_ACCESS_DENIED"
                elif (leaf.response.status_code == 503 and isinstance(detail, dict)
                        and detail.get("code") == "ORGANIZATION_ACCESS_UNAVAILABLE" and isinstance(detail.get("reason"), str) and detail.get("reason") in {
                            "SOURCE_EXPIRED", "SOURCE_UNREACHABLE", "SOURCE_INVALID", "SOURCE_MISSING", "SOURCE_ROLLBACK", "SOURCE_RETIRED",
                        }):
                    code = detail["reason"]
            outbox.record(call, phase, code)
        finally:
            call.release()

    async def _call_managed_tool(self, upstream: ProfileUpstream, tool_name: str, arguments: dict[str, Any], *, reserved: bool = False) -> CallToolResult:
        managed = upstream.managed
        if self.registry_client is None or managed is None:
            raise ToolRoutingError("MANAGED_CONTROL_PLANE_REQUIRED")
        identity = await self.registry_client.managed_report_identity(refresh_tokens=False)
        outbox = self._event_outbox()
        call = outbox.begin(managed, identity, reserved=reserved)
        try:
            managed.assert_current()

            async def authorize(inventory: ToolInventory) -> None:
                def observed(observation_id: str) -> None:
                    call.observation_id = observation_id
                    if any(tool.name == tool_name for tool in inventory.tools):
                        call.original_tool_name = tool_name

                def reported(proof: str) -> None:
                    call.reporting_proof = proof

                try:
                    await self.registry_client.authorize_inventory(managed, inventory, tool_name, observed=observed, reported=reported)
                except ManagedToolDenied:
                    if call.observation_id:
                        self._queue_managed_application(managed, call.observation_id)
                    raise
                managed.assert_current()
                if call.observation_id:
                    self._queue_managed_application(managed, call.observation_id)
                outbox.record(call, "dispatch_started")
                try:
                    managed.assert_current()
                except RegistryApiError:
                    call.handoff = False
                    raise

            result = await self.upstream_client.call_tool(upstream.config, tool_name, arguments, authorize=authorize)
        except BaseException as exc:
            leaf = unwrap_error(exc)
            if call.handoff:
                outbox.record(call, "outcome_unknown", "UPSTREAM_OUTCOME_UNKNOWN")
            elif isinstance(leaf, RegistryApiError) and str(leaf) in {"MANAGED_TOOL_DENIED", "MANAGED_AUTHORITY_EXPIRED"}:
                outbox.record(call, "denied", leaf.reason_code if isinstance(leaf, ManagedToolDenied) else str(leaf))
            else:
                code = str(leaf) if isinstance(leaf, RegistryApiError) and str(leaf) in {
                    "MANAGED_CONTEXT_MISMATCH", "MANAGED_RUNTIME_NOT_READY",
                } else "EVIDENCE_STORE_UNAVAILABLE" if isinstance(leaf, EvidenceStoreError) else "RUNTIME_START_FAILED"
                if isinstance(leaf, httpx.HTTPError):
                    code = "CONTROL_PLANE_UNAVAILABLE"
                outbox.record(call, "runtime_failed", code)
            raise
        else:
            outbox.record(call, "upstream_failed" if result.is_error else "succeeded", "UPSTREAM_FAILED" if result.is_error else None)
            return result
        finally:
            call.release()

    async def flush_managed_events(self) -> None:
        if self.registry_client is None or self._managed_upload_lock.locked():
            return
        async with self._managed_upload_lock:
            try:
                identity = await self.registry_client.managed_report_identity()
                outbox = self._event_outbox()
                for event in outbox.pending(identity):
                    try:
                        await self.registry_client.post_managed_runtime_event(event, identity)
                    except httpx.HTTPStatusError as exc:
                        status = exc.response.status_code
                        if status in {400, 403, 404, 409, 413, 422}:
                            outbox.reject(event["id"], f"REPORT_REJECTED_{status}")
                            continue
                        raise
                    outbox.acknowledge(event["id"])
                self._managed_report_error = None
            except Exception:
                # Exception text can contain URLs, headers or private receipts.
                self._managed_report_error = "REPORT_DELIVERY_UNAVAILABLE"

    async def run_managed_event_loop(self) -> None:
        while True:
            await self.flush_managed_events()
            await asyncio.sleep(5)

    def managed_event_status(self) -> dict[str, Any]:
        return {"enabled": self.managed_events_enabled(), **(self._managed_outbox.status() if self._managed_outbox else {}),
                "delivery_error": self._managed_report_error}

    async def list_resources(self, profile_name: str) -> list[Resource]:
        started = monotonic()
        try:
            upstreams = await self._profile_upstream_details(profile_name)
            resources: list[Resource] = []
            failures: list[tuple[str, Exception]] = []
            for upstream_name, upstream in upstreams.items():
                try:
                    if upstream.managed is not None:
                        raise ToolRoutingError("MANAGED_METHOD_NOT_AUTHORIZED")
                    upstream_resources = await self.upstream_client.list_resources(upstream.config)
                except Exception as exc:
                    failures.append((upstream_name, exc))
                    logger.warning("failed to list resources for upstream %s: %s", upstream_name, unwrap_error(exc))
                    continue
                for resource in upstream_resources:
                    resources.append(prefix_resource(upstream_name, resource))
            resources.sort(key=lambda item: (str(item.uri), item.name))
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
                    if upstream.managed is not None:
                        raise ToolRoutingError("MANAGED_METHOD_NOT_AUTHORIZED")
                    upstream_templates = await self.upstream_client.list_resource_templates(upstream.config)
                except Exception as exc:
                    failures.append((upstream_name, exc))
                    logger.warning(
                        "failed to list resource templates for upstream %s: %s", upstream_name, unwrap_error(exc)
                    )
                    continue
                for template in upstream_templates:
                    templates.append(prefix_resource_template(upstream_name, template))
            templates.sort(key=lambda item: (item.uri_template, item.name))
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
            if upstream.managed is not None:
                raise ToolRoutingError("MANAGED_METHOD_NOT_AUTHORIZED")
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

    async def _call_target(
        self, profile_name: str, proxied_tool_name: str,
    ) -> tuple[RoutedTool, ProfileUpstream | None]:
        profile = self._profile(profile_name)
        if self.config.local_proxy.enabled and self.config.local_proxy.sync_from_lightnow:
            if self.registry_client is None:
                raise ToolRoutingError("local proxy sync requires registry_api to be enabled")
            await self._ensure_runtime_registration()
            payload = await self.registry_client.fetch_profile_metadata(profile_name)
            servers = payload.get("servers")
            if not isinstance(servers, list):
                raise ToolRoutingError("INVALID_MANAGED_PROFILE")
            names: dict[str, str] = {}
            managed_names: set[str] = set()
            managed_aliases: set[str] = set()
            for item in servers:
                if not isinstance(item, dict) or not isinstance(item.get("alias"), str) or not RegistryApiClient._profile_server_enabled(item):
                    continue
                alias = item["alias"]
                if "connection_id" in item:
                    namespace = managed_tool_namespace(item.get("assignment_id"))
                    managed_names.add(namespace)
                    managed_aliases.add(alias)
                else:
                    namespace = alias
                    if reserved_managed_namespace(namespace):
                        raise ToolRoutingError("RESERVED_MANAGED_TOOL_NAMESPACE")
                if namespace in names:
                    raise ToolRoutingError("DUPLICATE_PROFILE_TOOL_NAMESPACE")
                names[namespace] = alias
            required = RegistryApiClient._admission_required(payload)
            routable = {name: None for name in names}
            if not required:
                routable.update({name: None for name in self.config.upstreams
                                 if name not in managed_aliases and not reserved_managed_namespace(name)})
                routable.update({item.name: None for item in profile.runtime_upstreams
                                 if item.name not in managed_aliases and not reserved_managed_namespace(item.name)})
            # Reserved UUID namespaces never fall back to a different instance,
            # including local overrides after detachment or alias reuse.
            namespace = proxied_tool_name.partition("__")[0]
            if reserved_managed_namespace(namespace):
                routable = {name: None for name in managed_names}
                if namespace not in managed_names:
                    raise RegistryApiError("MANAGED_ASSIGNMENT_UNAVAILABLE")
            routed = self.route_tool(proxied_tool_name, routable)
            if routed.upstream_name in names:
                resolved = await self.registry_client.fetch_profile_upstream(profile_name, names[routed.upstream_name], get_current_principal())
                if routed.upstream_name in managed_names and (resolved.managed is None or resolved.managed.tool_namespace != routed.upstream_name):
                    raise ToolRoutingError("MANAGED_CONTEXT_MISMATCH")
                return RoutedTool(resolved.name, routed.original_name), ProfileUpstream(config=resolved.config, server_name=resolved.server_name, managed=resolved.managed)
            if required:
                return routed, None
        else:
            routable = {**self.config.upstreams, **{item.name: None for item in profile.runtime_upstreams}}
            routed = self.route_tool(proxied_tool_name, routable)
        for runtime_upstream in profile.runtime_upstreams:
            if runtime_upstream.name == routed.upstream_name:
                if self.registry_client is None:
                    raise ToolRoutingError("profile uses runtime_upstreams but registry_api is not enabled")
                resolved = await self.registry_client.resolve_upstream(runtime_upstream, get_current_principal())
                return routed, ProfileUpstream(config=resolved.config, server_name=runtime_upstream.server)
        if routed.upstream_name in profile.upstreams:
            return routed, ProfileUpstream(config=self.config.upstreams[routed.upstream_name])
        return routed, None

    async def _ensure_runtime_registration(self) -> None:
        if self.device_heartbeat_enabled() and self.device_heartbeat_last_success_at is None:
            await self.send_device_heartbeat()

    async def _profile_upstreams(self, profile_name: str) -> dict[str, UpstreamConfig]:
        return {
            name: upstream.config for name, upstream in (await self._profile_upstream_details(profile_name)).items()
        }

    async def _profile_upstream_details(self, profile_name: str) -> dict[str, ProfileUpstream]:
        profile = self._profile(profile_name)
        canonical: dict[str, ProfileUpstream] = {}
        if self.config.local_proxy.enabled and self.config.local_proxy.sync_from_lightnow:
            if self.registry_client is None:
                raise ToolRoutingError("local proxy sync requires registry_api to be enabled")
            await self._ensure_runtime_registration()
            resolved_profile = await self.registry_client.fetch_profile(profile_name, get_current_principal())
            canonical = {item.name: ProfileUpstream(config=item.config, server_name=item.server_name, managed=item.managed)
                         for item in resolved_profile.upstreams}
            if resolved_profile.admission_required:
                return canonical
        if self.config.local_proxy.enabled and self.config.local_proxy.sync_from_lightnow and (
            any(reserved_managed_namespace(name) for name in profile.upstreams)
            or any(reserved_managed_namespace(item.name) for item in profile.runtime_upstreams)
        ):
            raise ToolRoutingError("RESERVED_MANAGED_TOOL_NAMESPACE")
        upstreams = {name: ProfileUpstream(config=self.config.upstreams[name]) for name in profile.upstreams}
        for runtime_upstream in profile.runtime_upstreams:
            if runtime_upstream.name in canonical:
                continue
            if self.registry_client is None:
                raise ToolRoutingError("profile uses runtime_upstreams but registry_api is not enabled")
            resolved = await self.registry_client.resolve_upstream(runtime_upstream, get_current_principal())
            upstreams[resolved.name] = ProfileUpstream(config=resolved.config, server_name=runtime_upstream.server)
        upstreams.update(canonical)
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
                "status": "error" if result.is_error else "success",
                "server_alias": routed.upstream_name,
                "server_name": upstream.server_name if upstream else None,
                "tool_name": proxied_tool_name,
                "original_tool_name": routed.original_name,
                "duration_ms": self._duration_ms(started),
                "request_bytes": tool_arguments_size(arguments),
                **runtime_request_metadata(
                    arguments,
                    request_meta,
                    capture_arguments=self.config.local_proxy.capture_tool_arguments and not ((upstream and upstream.managed) or reserved_managed_namespace(proxied_tool_name.partition("__")[0])),
                ),
                "upstream_transport": upstream.config.transport if upstream else None,
                "response_items_count": len(result.content),
                "response_bytes": call_tool_result_size(result),
                "response_content_type": first_call_tool_content_type(result),
            }
        )

    async def _emit_runtime_event(self, payload: dict[str, Any]) -> None:
        if self.registry_client is None or not self.config.local_proxy.telemetry_enabled:
            return
        request_client = get_current_mcp_client_context() or {}
        client_name = request_client.get("name") or self.client_context.name or self.config.local_proxy.client_name
        client_version = (
            request_client.get("version") or self.client_context.version or self.config.local_proxy.client_version
        )
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
                "device_installation_id": str(self.config.local_proxy.device_installation_id)
                if self.config.local_proxy.device_installation_id
                else None,
                "client_instance_id": str(self.config.local_proxy.client_instance_id)
                if self.config.local_proxy.client_instance_id
                else None,
                "mcp_method": mcp_method_for_event(str(payload.get("event_type", ""))),
            }.items()
            if value is not None
        }
        try:
            await self.registry_client.post_runtime_event(event)
        except Exception as exc:  # pragma: no cover - defensive logging only
            logger.warning("failed to post LightNow MCP runtime event: %s", exc)

    def device_heartbeat_enabled(self) -> bool:
        """Return whether this observed Local Proxy has enough data to report presence."""
        return bool(
            self.config.local_proxy.enabled
            and (self.config.local_proxy.telemetry_enabled or self.config.local_proxy.sync_from_lightnow)
            and self.registry_client is not None
            and self.config.local_proxy.device_installation_id
            and self.config.local_proxy.client_instance_id
            and self.config.local_proxy.device_hostname
        )

    async def send_device_heartbeat(self) -> bool:
        """Send one best-effort heartbeat without affecting MCP traffic."""
        if not self.device_heartbeat_enabled() or self.registry_client is None:
            return False
        local_proxy = self.config.local_proxy
        self.device_heartbeat_last_attempt_at = datetime.now(UTC)
        device_versions, runner_versions = heartbeat_version_fields(local_proxy)
        applications = self._pending_managed_applications() if local_proxy.sync_from_lightnow else {}
        payload = {
            "device": {
                "reported_hostname": local_proxy.device_hostname,
                "platform": local_proxy.device_platform,
                **device_versions,
            },
            "client": {
                "name": self.client_context.name or local_proxy.client_name,
                "version": self.client_context.version or local_proxy.client_version,
                "profile": local_proxy.profile,
                **({"managed_capabilities": [MANAGED_CAPABILITY]} if local_proxy.sync_from_lightnow else {}),
                **({"managed_applications": [{"observation_id": observation, "context_proof": managed.context_proof}
                                              for managed, observation in applications.values()]} if applications else {}),
                "runner_name": local_proxy.runner_name,
                "runner_version": __version__,
                **runner_versions,
                "transport": local_proxy.client_transport,
            },
        }
        try:
            result = await self.registry_client.post_device_heartbeat(
                str(local_proxy.device_installation_id),
                str(local_proxy.client_instance_id),
                payload,
            )
            for client in result.get("device", {}).get("clients", []):
                if client.get("client_instance_id") == str(local_proxy.client_instance_id):
                    self.registry_client.runtime_client_id = str(UUID(client["id"]))
            if local_proxy.sync_from_lightnow and self.registry_client.runtime_client_id is not None:
                if applications:
                    self._acknowledge_managed_applications(applications, result.get("managed_application_acks"))
                self._schedule_discoveries(result.get("managed_discovery_requests"))
        except Exception as exc:
            self.device_heartbeat_last_error = error_message(exc)
            logger.warning("failed to post LightNow device heartbeat: %s", exc)
            return False

        self.device_heartbeat_last_success_at = datetime.now(UTC)
        self.device_heartbeat_last_error = None
        return True

    async def run_device_heartbeat_loop(self) -> None:
        """Send immediately, then let each regular interval serve as the only retry."""
        try:
            while True:
                await self.send_device_heartbeat()
                await asyncio.sleep(self._heartbeat_interval())
        finally:
            tasks = list(self._discovery_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _heartbeat_interval(self) -> int:
        return MANAGED_HEARTBEAT_INTERVAL_SECONDS if self.config.local_proxy.sync_from_lightnow else DEVICE_HEARTBEAT_INTERVAL_SECONDS

    def _schedule_discoveries(self, requests: Any) -> None:
        if not isinstance(requests, list):
            return
        for request in requests[:20]:
            if len(self._discovery_tasks) >= 2:
                break
            if not isinstance(request, dict) or request.get("profile") != self.config.local_proxy.profile:
                continue
            try:
                attempt_id = str(UUID(request["id"]))
                if request.get("runtime_client_id") != self.registry_client.runtime_client_id:
                    continue
                expires = datetime.fromisoformat(request["expires_at"])
                if expires.tzinfo is None or expires <= datetime.now(UTC) or request.get("status") not in {"queued", "discovering"}:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            if attempt_id not in self._discovery_tasks:
                task = asyncio.create_task(self._run_discovery(request), name="lightnow-private-discovery")
                self._discovery_tasks[attempt_id] = task
                task.add_done_callback(lambda _task, key=attempt_id: self._discovery_tasks.pop(key, None))

    async def _run_discovery(self, request: dict[str, Any]) -> None:
        """A queue entry selects work, never authority or a substitute request."""
        if self.registry_client is None:
            return
        try:
            profile = self.config.local_proxy.profile
            metadata = await self.registry_client.fetch_profile_metadata(profile)
            assignment = next((item for item in metadata.get("servers", []) if isinstance(item, dict)
                               and item.get("assignment_id") == request.get("assignment_id")
                               and item.get("connection_id") == request.get("connection_id")), None)
            if assignment is None or not isinstance(assignment.get("alias"), str):
                return
            resolved = await self.registry_client.fetch_profile_upstream(profile, assignment["alias"], None)
            managed = resolved.managed
            if managed is None:
                return
            for field in ("connection_id", "connection_revision", "profile_id", "profile_revision", "policy_revision", "profile",
                          "assignment_id", "assignment_revision", "runtime_client_id", "credential_binding_id", "credential_revision"):
                value = getattr(managed, field)
                if isinstance(value, UUID):
                    value = str(value)
                if field not in request or value != request[field]:
                    return
            if not isinstance(request.get("etag"), str):
                return
            expires = datetime.fromisoformat(request["expires_at"])
            if expires.tzinfo is None or expires <= datetime.now(UTC):
                return
            managed.assert_current()
            started = monotonic()
            try:
                async with asyncio.timeout(30):
                    async with self.upstream_client.session(resolved.config) as client:
                        inventory = await self.upstream_client.discover_tools(client)
            except Exception as exc:
                leaf = unwrap_error(exc)
                diagnostic = diagnostic_for_exception(leaf)
                if isinstance(leaf, ToolDiscoveryError):
                    status, reason = "partial", leaf.reason_code
                else:
                    status, reason = {
                        "configuration": ("configuration_required", "CONFIGURATION_REQUIRED"),
                        "authentication": ("authentication_required", "AUTHENTICATION_REQUIRED"),
                        "authorization": ("authentication_required", "AUTHENTICATION_REQUIRED"),
                        "timeout": ("partial", "DISCOVERY_TIMED_OUT"),
                        "runtime": ("startup_failed", "RUNTIME_START_FAILED"),
                    }.get(diagnostic.kind, ("connection_failed", "CONNECTION_FAILED"))
                await self.registry_client.report_discovery(managed, request["id"], request["etag"], {
                    "status": status, "reason_code": reason, "page_count": 0,
                    "duration_ms": min(30000, self._duration_ms(started)),
                })
                return
            contribution = request.get("contribution")
            preview_options = {}
            if isinstance(contribution, dict) and contribution.get("state") == "preparing":
                preview_options = {"prepare_contribution": True, "contribution_template": contribution.get("template"),
                                   "contribution_inputs": resolved.contribution_inputs}
            await self.registry_client.report_inventory(managed, inventory, request["id"], request["etag"], **preview_options)
            self._queue_managed_application(managed, request["id"])
        except Exception:
            # No untrusted tool metadata, URLs, credentials or raw errors in logs.
            logger.warning("private discovery could not be reported under current authority")

    def _queue_managed_application(self, managed: ManagedRuntime, observation: str) -> None:
        managed.assert_current()
        observation = str(UUID(observation))
        key = str(managed.assignment_id)
        self._pending_managed_applications()
        if key not in self._managed_applications and len(self._managed_applications) >= 2000:
            raise RegistryApiError("MANAGED_APPLICATION_CAPACITY_EXCEEDED")
        self._managed_applications[key] = (managed, observation)

    def _pending_managed_applications(self) -> dict[str, tuple[ManagedRuntime, str]]:
        for key, (managed, _) in list(self._managed_applications.items()):
            try:
                managed.assert_current()
            except RegistryApiError:
                del self._managed_applications[key]
                self._managed_application_error = "MANAGED_APPLICATION_EXPIRED"
        return dict(list(self._managed_applications.items())[:20])

    def _acknowledge_managed_applications(self, sent: dict[str, tuple[ManagedRuntime, str]], response: Any) -> None:
        codes = {"OBSERVATION_UNAVAILABLE", "OBSERVATION_INCOMPLETE", "OBSERVATION_UNBOUND", "CONTEXT_CHANGED",
                 "ACCESS_DENIED", "CAPACITY_EXCEEDED", "RUNTIME_INCOMPATIBLE"}
        expected = {observation for _, observation in sent.values()}
        if (not isinstance(response, list) or len(response) != len(expected)
                or any(not isinstance(item, dict) or not isinstance(item.get("observation_id"), str)
                       or item.get("observation_id") not in expected
                       or item.get("status") not in ("accepted", "rejected")
                       or (item.get("status") == "accepted" and item.get("code") is not None)
                       or (item.get("status") == "rejected" and (not isinstance(item.get("code"), str)
                                                                 or item.get("code") not in codes)) for item in response)
                or {item["observation_id"] for item in response} != expected):
            self._managed_application_error = "INVALID_MANAGED_APPLICATION_ACK"
            return
        self._managed_application_error = next((item["code"] for item in response if item["status"] == "rejected"), None)
        for key, application in sent.items():
            if self._managed_applications.get(key) == application:
                del self._managed_applications[key]

    def device_heartbeat_status(self) -> dict[str, Any]:
        """Return non-secret local diagnostics for the last heartbeat attempt."""
        return {
            "enabled": self.device_heartbeat_enabled(),
            "interval_seconds": self._heartbeat_interval(),
            "last_attempt_at": self.device_heartbeat_last_attempt_at.isoformat()
            if self.device_heartbeat_last_attempt_at
            else None,
            "last_success_at": self.device_heartbeat_last_success_at.isoformat()
            if self.device_heartbeat_last_success_at
            else None,
            "last_error": self.device_heartbeat_last_error,
            "managed_applications_pending": len(self._managed_applications),
            "managed_application_error": self._managed_application_error,
        }

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
        failed_upstreams = proxy_health_failures(report)
        await self._emit_runtime_event(
            {
                "profile": self.config.local_proxy.profile,
                "event_type": "proxy_health",
                "status": "success" if health_status == "healthy" else "error",
                "proxy_health_status": health_status
                if health_status in {"healthy", "degraded", "failed"}
                else "failed",
                "proxy_upstreams": int(summary.get("upstreams") or 0),
                "proxy_healthy_upstreams": int(summary.get("healthy_upstreams") or 0),
                "proxy_failed_upstreams": int(summary.get("failed_upstreams") or 0),
                "proxy_tool_count": int(summary.get("tools") or 0),
                "proxy_health_failures": failed_upstreams or None,
            }
        )

    def _duration_ms(self, started: float) -> int:
        return max(0, int((monotonic() - started) * 1000))

    def _list_tools_error_message(self, failures: list[tuple[str, Exception]]) -> str:
        return "; ".join(f"{name}: {error_type_name(exc)}" for name, exc in failures)[:500]


def reserved_managed_namespace(value: str) -> bool:
    return re.fullmatch(r"ln_[0-9a-f]{32}", value.partition("__")[0]) is not None


def prefix_tool(upstream_name: str, tool: Tool, *, managed: ManagedRuntime | None = None) -> Tool:
    data = tool.model_dump(exclude_none=True, by_alias=True)
    data = copy.deepcopy(data)
    namespace = managed.tool_namespace if managed is not None else upstream_name
    data["name"] = f"{namespace}__{tool.name}"
    description = data.get("description") or ""
    data["description"] = f"[{upstream_name}] {description}".strip()
    meta = dict(data.get("_meta") or {})
    meta["lightnow_proxy_upstream"] = upstream_name
    meta["lightnow_proxy_original_name"] = tool.name
    if managed is not None:
        meta["lightnow_proxy_connection_id"] = str(managed.connection_id)
        meta["lightnow_proxy_assignment_id"] = str(managed.assignment_id)
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
    meta["lightnow_proxy_original_uri_template"] = template.uri_template
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


def runtime_request_metadata(
    arguments: dict[str, Any] | None,
    request_meta: dict[str, Any] | None,
    *,
    capture_arguments: bool = True,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "argument_keys": sorted((arguments or {}).keys()),
        "arguments_captured": capture_arguments,
    }
    if capture_arguments:
        metadata["arguments"] = sanitize_runtime_arguments(arguments or {})
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


def sanitize_runtime_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {key: sanitize_runtime_argument_value(value, key=key) for key, value in arguments.items()}


def sanitize_runtime_argument_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and sensitive_argument_key(key):
        return REDACTED_ARGUMENT_VALUE
    if isinstance(value, Mapping):
        return {
            str(nested_key): sanitize_runtime_argument_value(nested_value, key=str(nested_key))
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [sanitize_runtime_argument_value(item) for item in value]
    return value


def sensitive_argument_key(key: str) -> bool:
    camel_separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    normalized = re.sub(r"[^a-z0-9]", "", camel_separated.lower())
    parts = {part for part in re.split(r"[^a-z0-9]+", camel_separated.lower()) if part}
    return (
        normalized in SENSITIVE_ARGUMENT_KEY_PARTS
        or any(normalized.endswith(part) for part in SENSITIVE_ARGUMENT_KEY_PARTS)
        or bool(parts & SENSITIVE_ARGUMENT_KEY_PARTS)
    )


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


def proxy_health_failures(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    profiles = report.get("profiles")
    if not isinstance(profiles, list):
        return failures

    for profile in profiles:
        if not isinstance(profile, Mapping):
            continue
        profile_name = profile.get("name") if isinstance(profile.get("name"), str) else None
        upstreams = profile.get("upstreams")
        if not isinstance(upstreams, list):
            continue
        for upstream in upstreams:
            if not isinstance(upstream, Mapping) or upstream.get("status") == "healthy":
                continue
            name = upstream.get("name")
            if not isinstance(name, str) or not name:
                continue
            failure = {
                "profile": profile_name,
                "name": name,
                "transport": upstream.get("transport") if isinstance(upstream.get("transport"), str) else None,
                "server_name": upstream.get("server_name") if isinstance(upstream.get("server_name"), str) else None,
                "status": upstream.get("status") if isinstance(upstream.get("status"), str) else "error",
                "duration_ms": upstream.get("duration_ms") if isinstance(upstream.get("duration_ms"), int) else None,
                "error_type": upstream.get("error_type") if isinstance(upstream.get("error_type"), str) else None,
                "error_message": upstream.get("error_message")
                if isinstance(upstream.get("error_message"), str)
                else None,
                "diagnostic_code": upstream.get("diagnostic_code")
                if isinstance(upstream.get("diagnostic_code"), str)
                else None,
                "diagnostic_kind": upstream.get("diagnostic_kind")
                if isinstance(upstream.get("diagnostic_kind"), str)
                else None,
                "diagnostic_summary": upstream.get("diagnostic_summary")
                if isinstance(upstream.get("diagnostic_summary"), str)
                else None,
                "remediation": upstream.get("remediation") if isinstance(upstream.get("remediation"), str) else None,
            }
            failures.append({key: value for key, value in failure.items() if value is not None})

    return failures[:20]


def resource_result_size(result: ReadResourceResult) -> int:
    return len(result.model_dump_json(exclude_none=True).encode("utf-8"))


def first_call_tool_content_type(result: CallToolResult) -> str | None:
    if not result.content:
        return None
    return getattr(result.content[0], "type", None)


def first_resource_mime_type(result: ReadResourceResult) -> str | None:
    if not result.contents:
        return None
    return getattr(result.contents[0], "mime_type", None)


def mcp_method_for_event(event_type: str) -> str | None:
    return {
        "list_tools": "tools/list",
        "call_tool": "tools/call",
        "list_resources": "resources/list",
        "read_resource": "resources/read",
        "list_resource_templates": "resources/templates/list",
    }.get(event_type)
