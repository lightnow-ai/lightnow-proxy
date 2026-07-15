from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import asyncio
from contextlib import suppress
from ipaddress import ip_address
import logging
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp import types
from mcp.types import CallToolResult, ReadResourceResult, TextContent
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send
import structlog

from lightnow_proxy import __version__
from lightnow_proxy.auth import AuthError, TokenVerifier, has_required_group
from lightnow_proxy.config import ProxyConfig
from lightnow_proxy.health import build_health_report
from lightnow_proxy.request_context import current_principal
from lightnow_proxy.router import ToolRouter, ToolRoutingError

logger = structlog.get_logger("lightnow_proxy")


class ProfileMCPApp:
    def __init__(
        self,
        profile_name: str,
        config: ProxyConfig,
        router: ToolRouter,
        verifier: TokenVerifier,
    ):
        self.profile_name = profile_name
        self.config = config
        self.router = router
        self.verifier = verifier
        self.server = self._build_server()
        self.session_manager = StreamableHTTPSessionManager(
            app=self.server,
            json_response=False,
            stateless=True,
            security_settings=transport_security_settings(config, local_only=False),
        )

    def _build_server(self) -> Server:
        server = Server(f"lightnow-proxy-{self.profile_name}", version=__version__)

        @server.list_tools()
        async def list_tools() -> list:
            return await self.router.list_tools(self.profile_name)

        @server.call_tool(validate_input=False)
        async def call_tool(
            name: str,
            arguments: dict[str, Any] | None = None,
            request_meta: dict[str, Any] | None = None,
        ) -> CallToolResult:
            try:
                return await self.router.call_tool(self.profile_name, name, arguments or {}, request_meta)
            except ToolRoutingError as exc:
                return CallToolResult(content=[TextContent(type="text", text=str(exc))], isError=True)

        @server.list_resources()
        async def list_resources() -> list:
            return await self.router.list_resources(self.profile_name)

        @server.list_resource_templates()
        async def list_resource_templates() -> list:
            return await self.router.list_resource_templates(self.profile_name)

        @server.read_resource()
        async def read_resource(uri: str) -> ReadResourceResult:
            return await self.router.read_resource(self.profile_name, uri)

        register_unvalidated_call_tool_handler(server, call_tool)
        return server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        profile = self.config.profiles[self.profile_name]
        try:
            principal = await self.verifier.verify_headers(Headers(scope=scope))
            if not has_required_group(principal, profile.required_groups):
                logger.warning(
                    "mcp_profile_forbidden",
                    profile=self.profile_name,
                    subject=principal.subject,
                    username=principal.username,
                    required_groups=profile.required_groups,
                )
                await forbidden(scope, receive, send)
                return
            scope.setdefault("state", {})
            scope["state"]["principal"] = principal
        except AuthError as exc:
            logger.warning("mcp_profile_unauthorized", profile=self.profile_name, reason=str(exc))
            await unauthorized(scope, receive, send, str(exc))
            return
        except Exception as exc:
            logger.warning("mcp_profile_auth_failed", profile=self.profile_name, reason=exc.__class__.__name__)
            await unauthorized(scope, receive, send, "invalid token")
            return

        principal_token = current_principal.set(principal)
        try:
            await self.session_manager.handle_request(scope, receive, send)
        finally:
            current_principal.reset(principal_token)


class LocalProxyMCPApp:
    def __init__(
        self,
        config: ProxyConfig,
        router: ToolRouter,
    ):
        self.config = config
        self.router = router
        self.profile_name = config.local_proxy.profile
        self.server = self._build_server()
        self.session_manager = StreamableHTTPSessionManager(
            app=self.server,
            json_response=False,
            stateless=True,
            security_settings=transport_security_settings(config, local_only=True),
        )

    def _build_server(self) -> Server:
        server = Server("lightnow-local-proxy", version=__version__)

        @server.list_tools()
        async def list_tools() -> list:
            return await self.router.list_tools(self.profile_name)

        @server.call_tool(validate_input=False)
        async def call_tool(
            name: str,
            arguments: dict[str, Any] | None = None,
            request_meta: dict[str, Any] | None = None,
        ) -> CallToolResult:
            try:
                return await self.router.call_tool(self.profile_name, name, arguments or {}, request_meta)
            except ToolRoutingError as exc:
                return CallToolResult(content=[TextContent(type="text", text=str(exc))], isError=True)

        @server.list_resources()
        async def list_resources() -> list:
            return await self.router.list_resources(self.profile_name)

        @server.list_resource_templates()
        async def list_resource_templates() -> list:
            return await self.router.list_resource_templates(self.profile_name)

        @server.read_resource()
        async def read_resource(uri: str) -> ReadResourceResult:
            return await self.router.read_resource(self.profile_name, uri)

        register_unvalidated_call_tool_handler(server, call_tool)
        return server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.session_manager.handle_request(scope, receive, send)


def transport_security_settings(config: ProxyConfig, *, local_only: bool) -> TransportSecuritySettings:
    """Build MCP HTTP transport security settings.

    MCP Streamable HTTP servers must validate Host and Origin headers. Local
    Proxy mode is unauthenticated and must remain loopback-only; profile proxy
    mode may be deployed behind a configured public URL and still uses bearer
    auth before dispatching MCP requests.
    """
    hosts = loopback_allowed_hosts()
    origins = loopback_allowed_origins()
    add_allowed_host(hosts, config.server.host, require_loopback=local_only)

    public = urlparse(config.server.public_url)
    if public.hostname:
        add_allowed_host(hosts, public.netloc, require_loopback=local_only)
        add_allowed_host(hosts, public.hostname, require_loopback=local_only)
        if public.scheme in {"http", "https"} and (not local_only or is_loopback_host(public.hostname)):
            origins.add(f"{public.scheme}://{public.netloc}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


def loopback_allowed_hosts() -> set[str]:
    return {"127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"}


def loopback_allowed_origins() -> set[str]:
    return {
        f"{scheme}://{host}"
        for scheme in ("http", "https")
        for host in ("127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*")
    }


def add_allowed_host(hosts: set[str], host: str, *, require_loopback: bool) -> None:
    normalized = normalize_allowed_host(host)
    if normalized is None:
        return
    if require_loopback and not is_loopback_host(normalized):
        return
    hosts.add(normalized)
    hosts.add(f"{normalized}:*")


def normalize_allowed_host(host: str) -> str | None:
    if not host:
        return None
    raw_host = host.rsplit("@", 1)[-1].strip()
    if not raw_host:
        return None
    parsed = urlparse(f"//{raw_host}")
    hostname = parsed.hostname or raw_host
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def is_loopback_host(host: str) -> bool:
    normalized = normalize_allowed_host(host)
    if normalized is None:
        return False
    if normalized == "localhost":
        return True
    candidate = normalized.strip("[]")
    try:
        return ip_address(candidate).is_loopback
    except ValueError:
        return False


async def unauthorized(scope: Scope, receive: Receive, send: Send, detail: str) -> None:
    response = JSONResponse(
        {"error": "unauthorized", "detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )
    await response(scope, receive, send)


async def forbidden(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse({"error": "forbidden"}, status_code=403)
    await response(scope, receive, send)


async def status(_request: Request, config: ProxyConfig, router: ToolRouter) -> Response:
    """Return non-secret Local Proxy runtime status for local health checks."""
    mode = "local_proxy" if config.local_proxy.enabled else "profile_proxy"
    return JSONResponse(
        {
            "status": "ok",
            "mode": mode,
            "runner": {
                "name": config.local_proxy.runner_name,
                "version": __version__,
            },
            "local_proxy": {
                "enabled": config.local_proxy.enabled,
                "profile": config.local_proxy.profile if config.local_proxy.enabled else None,
                "path": config.local_proxy.path if config.local_proxy.enabled else None,
                "sync_from_lightnow": config.local_proxy.sync_from_lightnow,
                "client_name": config.local_proxy.client_name,
                "client_version": config.local_proxy.client_version,
                "client_transport": config.local_proxy.client_transport,
                "telemetry_enabled": config.local_proxy.telemetry_enabled,
                "policy_mode": config.local_proxy.policy_mode,
                "allow_unmanaged_client_servers": config.local_proxy.allow_unmanaged_client_servers,
                "device_heartbeat": device_heartbeat_status(router),
            },
            "registry_api": {
                "enabled": bool(config.registry_api and config.registry_api.enabled),
                "use_cli_session": bool(config.registry_api and config.registry_api.use_cli_session),
            },
            "profiles": sorted(config.profiles.keys()),
            "static_upstreams": sorted(config.upstreams.keys()),
        }
    )


def create_app(
    config: ProxyConfig, router: ToolRouter | None = None, verifier: TokenVerifier | None = None
) -> Starlette:
    logging.basicConfig(level=logging.INFO)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
    )

    router = router or ToolRouter(config)
    verifier = verifier or TokenVerifier(config.auth)
    local_proxy_app = LocalProxyMCPApp(config, router) if config.local_proxy.enabled else None
    profile_apps = {}
    if local_proxy_app is None:
        profile_apps = {
            profile_name: ProfileMCPApp(profile_name, config, router, verifier)
            for profile_name in sorted(config.profiles.keys())
        }

    async def status_endpoint(request: Request) -> Response:
        if request.query_params.get("check") == "upstreams":
            report = await build_health_report(config, router=router)
            await router.emit_health_event(report)
            return JSONResponse(report)
        return await status(request, config, router)

    routes = [
        Route("/status", status_endpoint, methods=["GET"]),
    ]
    if local_proxy_app is not None:
        routes.append(Route(config.local_proxy.path, endpoint=local_proxy_app))
    for profile_name, profile_app in profile_apps.items():
        routes.append(Route(f"/profiles/{profile_name}/mcp", endpoint=profile_app))

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            heartbeat_task: asyncio.Task[None] | None = None
            if local_proxy_app is not None:
                await stack.enter_async_context(local_proxy_app.session_manager.run())
                await emit_lifecycle_event(router, "runner_started")
                if device_heartbeat_enabled(router):
                    heartbeat_task = asyncio.create_task(
                        router.run_device_heartbeat_loop(),
                        name="lightnow-device-heartbeat",
                    )
            for profile_app in profile_apps.values():
                await stack.enter_async_context(profile_app.session_manager.run())
            try:
                yield
            finally:
                if local_proxy_app is not None:
                    await emit_lifecycle_event(router, "runner_stopped")
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat_task

    return Starlette(routes=routes, lifespan=lifespan)


async def emit_lifecycle_event(router: Any, event_type: str) -> None:
    emitter = getattr(router, "emit_lifecycle_event", None)
    if emitter is not None:
        await emitter(event_type)


def device_heartbeat_enabled(router: Any) -> bool:
    checker = getattr(router, "device_heartbeat_enabled", None)
    return bool(checker()) if checker is not None else False


def device_heartbeat_status(router: Any) -> dict[str, Any]:
    reporter = getattr(router, "device_heartbeat_status", None)
    if reporter is not None:
        return reporter()
    return {
        "enabled": False,
        "interval_seconds": 120,
        "last_attempt_at": None,
        "last_success_at": None,
        "last_error": None,
    }


def register_unvalidated_call_tool_handler(server: Server, handler_func) -> None:
    async def handler(req: types.CallToolRequest):
        request_meta = (
            req.params.meta.model_dump(mode="json", by_alias=True, exclude_none=True) if req.params.meta else None
        )
        result = await handler_func(req.params.name, req.params.arguments or {}, request_meta)
        return types.ServerResult(result)

    server.request_handlers[types.CallToolRequest] = handler
