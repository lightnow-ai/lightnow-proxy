from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import logging
from typing import Any, AsyncIterator

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
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


async def status(_request: Request, config: ProxyConfig) -> Response:
    """Return non-secret Local Proxy runtime status for local health checks."""
    mode = "local_proxy" if config.local_proxy.enabled else "profile_proxy"
    return JSONResponse(
        {
            "status": "ok",
            "mode": mode,
            "runner": {
                "name": config.local_proxy.runner_name,
                "version": config.local_proxy.runner_version,
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
            },
            "registry_api": {
                "enabled": bool(config.registry_api and config.registry_api.enabled),
                "use_cli_session": bool(config.registry_api and config.registry_api.use_cli_session),
            },
            "profiles": sorted(config.profiles.keys()),
            "static_upstreams": sorted(config.upstreams.keys()),
        }
    )


def create_app(config: ProxyConfig, router: ToolRouter | None = None, verifier: TokenVerifier | None = None) -> Starlette:
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
            return JSONResponse(await build_health_report(config))
        return await status(request, config)

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
            if local_proxy_app is not None:
                await stack.enter_async_context(local_proxy_app.session_manager.run())
                await emit_lifecycle_event(router, "runner_started")
            for profile_app in profile_apps.values():
                await stack.enter_async_context(profile_app.session_manager.run())
            try:
                yield
            finally:
                if local_proxy_app is not None:
                    await emit_lifecycle_event(router, "runner_stopped")

    return Starlette(routes=routes, lifespan=lifespan)


async def emit_lifecycle_event(router: Any, event_type: str) -> None:
    emitter = getattr(router, "emit_lifecycle_event", None)
    if emitter is not None:
        await emitter(event_type)


def register_unvalidated_call_tool_handler(server: Server, handler_func) -> None:
    async def handler(req: types.CallToolRequest):
        request_meta = req.params.meta.model_dump(mode="json", by_alias=True, exclude_none=True) if req.params.meta else None
        result = await handler_func(req.params.name, req.params.arguments or {}, request_meta)
        return types.ServerResult(result)

    server.request_handlers[types.CallToolRequest] = handler
