from __future__ import annotations

import argparse
import anyio
import json
import os

from mcp.server.stdio import stdio_server
import uvicorn

from lightnow_proxy import __version__
from lightnow_proxy.app import LocalProxyMCPApp, create_app
from lightnow_proxy.capture import capture_enabled, capture_read_stream, initialize_capture_file, initialize_client_context
from lightnow_proxy.config import ProxyConfig, load_config
from lightnow_proxy.health import build_health_report, format_health_summary, health_exit_code
from lightnow_proxy.router import ToolRouter


def main() -> None:
    parser = argparse.ArgumentParser(description="LightNow MCP proxy")
    parser.add_argument(
        "--version",
        action="version",
        version=f"lightnow-proxy {__version__}",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("LIGHTNOW_PROXY_CONFIG", "config.example.yaml"),
        help="Path to the LightNow Proxy YAML config.",
    )
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=os.environ.get("LIGHTNOW_PROXY_TRANSPORT", os.environ.get("MCP_PROXY_TRANSPORT", "http")),
        help="Client-facing transport to serve.",
    )
    parser.add_argument(
        "--capture-path",
        default=os.environ.get("LIGHTNOW_PROXY_CAPTURE_PATH", os.environ.get("MCP_PROXY_CAPTURE_PATH")),
        help="Optional text file for redacted inbound MCP initialize/request capture.",
    )
    parser.add_argument(
        "--warm-tools-cache",
        action="store_true",
        help="Fetch the local proxy profile tool list once, update the cache, and exit.",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Check the configured proxy profile and upstream MCP servers, then exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON for --health.",
    )
    args = parser.parse_args()

    config_path = args.config
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        parser.error(f"config file not found: {config_path} (set --config or LIGHTNOW_PROXY_CONFIG)")
    if args.health:
        report = anyio.run(build_and_emit_health_report, config)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(format_health_summary(report))
        raise SystemExit(health_exit_code(report))
    if args.warm_tools_cache:
        anyio.run(warm_tools_cache, config)
        return
    if args.transport == "stdio":
        anyio.run(run_stdio, config, args.capture_path, config_path)
        return

    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port)


async def build_and_emit_health_report(config: ProxyConfig) -> dict:
    router = ToolRouter(config)
    report = await build_health_report(config, router=router)
    await router.emit_health_event(report)
    return report


async def warm_tools_cache(config: ProxyConfig) -> None:
    if not config.local_proxy.enabled:
        raise RuntimeError("tools cache warming requires local_proxy.enabled=true")
    router = ToolRouter(config)
    tools = await router.list_tools(config.local_proxy.profile)
    print(f"Warmed tools cache for profile {config.local_proxy.profile}: {len(tools)} tools")


async def run_stdio(config: ProxyConfig, capture_path: str | None = None, config_path: str = "") -> None:
    if not config.local_proxy.enabled:
        raise RuntimeError("stdio transport requires local_proxy.enabled=true")

    router = ToolRouter(config)
    server = LocalProxyMCPApp(config, router).server

    def observe_initialize(session_message) -> None:
        context = initialize_client_context(session_message)
        if context is not None:
            router.update_client_context(
                name=context.get("name"),
                version=context.get("version"),
                capability_keys=context.get("capability_keys"),
            )

    async with stdio_server() as (read_stream, write_stream):
        active_capture_path = capture_path if capture_enabled(capture_path) else None
        if active_capture_path is not None:
            initialize_capture_file(active_capture_path, config_path)
        captured_read_stream, forward_messages = await capture_read_stream(
            read_stream,
            active_capture_path,
            observe_initialize,
        )
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(forward_messages)
            await router.emit_lifecycle_event("runner_started")
            try:
                await server.run(
                    captured_read_stream,
                    write_stream,
                    server.create_initialization_options(),
                    stateless=True,
                )
            finally:
                await router.emit_lifecycle_event("runner_stopped")


if __name__ == "__main__":
    main()
