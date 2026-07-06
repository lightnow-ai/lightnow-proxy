from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
from dataclasses import asdict
from datetime import UTC, datetime
import io
import logging
from typing import Any

from lightnow_proxy import __version__
from lightnow_proxy.config import ProxyConfig
from lightnow_proxy.router import ToolRouter, error_message, error_type_name
from lightnow_proxy.upstream import UpstreamMCPClient


async def build_health_report(config: ProxyConfig, router: ToolRouter | None = None) -> dict[str, Any]:
    router = router or ToolRouter(config, upstream_client=UpstreamMCPClient(suppress_stdio_stderr=True))
    mode = "local_proxy" if config.local_proxy.enabled else "profile_proxy"
    profile_names = [config.local_proxy.profile] if config.local_proxy.enabled else sorted(config.profiles.keys())
    profiles: list[dict[str, Any]] = []
    all_upstreams: list[dict[str, Any]] = []

    for profile_name in profile_names:
        profile_payload: dict[str, Any] = {
            "name": profile_name,
            "status": "healthy",
            "upstreams": [],
        }
        try:
            with quiet_mcp_stdio_logs():
                upstreams = await router.profile_upstream_health(profile_name)
        except Exception as exc:
            profile_payload.update(
                {
                    "status": "failed",
                    "error_type": error_type_name(exc),
                    "error_message": error_message(exc),
                }
            )
            profiles.append(profile_payload)
            continue

        upstream_payloads = [asdict(upstream) for upstream in upstreams]
        if not upstream_payloads:
            profile_payload["status"] = "degraded"
            profile_payload["warning"] = "profile has no upstream MCP servers"
        elif any(upstream["status"] != "healthy" for upstream in upstream_payloads):
            profile_payload["status"] = "degraded"
        profile_payload["upstreams"] = upstream_payloads
        profiles.append(profile_payload)
        all_upstreams.extend(upstream_payloads)

    failed_profiles = [profile for profile in profiles if profile["status"] == "failed"]
    degraded_profiles = [profile for profile in profiles if profile["status"] == "degraded"]
    failed_upstreams = [upstream for upstream in all_upstreams if upstream["status"] != "healthy"]
    status = "healthy"
    if failed_profiles:
        status = "failed"
    elif degraded_profiles or failed_upstreams:
        status = "degraded"

    return {
        "status": status,
        "generated_at": datetime.now(UTC).isoformat(),
        "proxy": {
            "name": "lightnow-proxy",
            "version": __version__,
        },
        "mode": mode,
        "local_proxy": {
            "enabled": config.local_proxy.enabled,
            "profile": config.local_proxy.profile if config.local_proxy.enabled else None,
            "path": config.local_proxy.path if config.local_proxy.enabled else None,
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
        "summary": {
            "profiles": len(profiles),
            "upstreams": len(all_upstreams),
            "healthy_upstreams": len(all_upstreams) - len(failed_upstreams),
            "failed_upstreams": len(failed_upstreams),
            "tools": sum(int(upstream.get("tool_count") or 0) for upstream in all_upstreams),
        },
        "profiles": profiles,
    }


def health_exit_code(report: dict[str, Any]) -> int:
    status = report.get("status")
    if status == "healthy":
        return 0
    if status == "degraded":
        return 2
    return 1


def format_health_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        f"LightNow Proxy health: {report.get('status', 'unknown')}",
        (
            f"Profiles: {summary.get('profiles', 0)} | "
            f"Upstreams: {summary.get('healthy_upstreams', 0)}/{summary.get('upstreams', 0)} healthy | "
            f"Tools: {summary.get('tools', 0)}"
        ),
    ]
    for profile in report.get("profiles", []):
        lines.append(f"- profile {profile.get('name')}: {profile.get('status')}")
        for upstream in profile.get("upstreams", []):
            detail = f"  - {upstream.get('name')} ({upstream.get('transport')}): {upstream.get('status')}"
            if upstream.get("status") == "healthy":
                detail += f", {upstream.get('tool_count', 0)} tools"
            else:
                detail += f", {upstream.get('error_type')}: {upstream.get('error_message')}"
            lines.append(detail)
    return "\n".join(lines)


@contextmanager
def quiet_mcp_stdio_logs():
    logger = logging.getLogger("mcp.client.stdio")
    previous_level = logger.level
    logger.setLevel(logging.CRITICAL + 1)
    try:
        with redirect_stderr(io.StringIO()):
            yield
    finally:
        logger.setLevel(previous_level)
