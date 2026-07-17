"""Read passive LightNow update state without performing network or package operations."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from lightnow_proxy.config import LocalProxyConfig

INSTALL_METHODS = {"homebrew", "pipx", "uv", "unknown"}
UPDATE_STATUSES = {"current", "outdated", "ahead", "unknown"}


def detect_install_method(executable: str | None = None) -> str:
    """Identify supported isolated installers from the executable's resolved path."""
    candidate = Path(executable or sys.argv[0]).expanduser()
    if not candidate.is_absolute():
        located = shutil.which(str(candidate))
        if located is not None:
            candidate = Path(located)
    try:
        path = str(candidate.resolve()).lower()
    except OSError:
        path = str(candidate).lower()
    if "/cellar/lightnow-proxy/" in path or "/homebrew/" in path:
        return "homebrew"
    if "/pipx/venvs/lightnow-proxy/" in path or "\\pipx\\venvs\\lightnow-proxy\\" in path:
        return "pipx"
    if "/uv/tools/lightnow-proxy/" in path or "\\uv\\tools\\lightnow-proxy\\" in path:
        return "uv"
    return "unknown"


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _package(state: dict[str, Any], name: str) -> dict[str, Any]:
    packages = state.get("packages")
    if not isinstance(packages, dict):
        return {}
    package = packages.get(name)
    return package if isinstance(package, dict) else {}


def load_update_state(path: str) -> dict[str, Any]:
    """Return a validated-enough state snapshot; malformed state is treated as absent."""
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return {}
    return payload


def _status(value: Any) -> str:
    return value if value in UPDATE_STATUSES else "unknown"


def _method(value: Any, fallback: str = "unknown") -> str:
    return value if value in INSTALL_METHODS else fallback


def heartbeat_version_fields(config: LocalProxyConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build nullable API fields using local filesystem metadata only, without network or package-manager access."""
    state = load_update_state(config.update_state_path)
    checked_at = _string(state.get("checked_at"))
    cli = _package(state, "lightnow-cli")
    proxy = _package(state, "lightnow-proxy")

    cli_version = _string(cli.get("installed_version")) or config.cli_version
    cli_method = _method(cli.get("install_method"), config.cli_install_method)
    proxy_method = _method(proxy.get("install_method"), detect_install_method())

    return (
        {
            "cli_version": cli_version,
            "cli_install_method": cli_method,
            "cli_latest_version": _string(cli.get("latest_version")),
            "cli_update_status": _status(cli.get("status")),
            "cli_update_checked_at": checked_at,
        },
        {
            "runner_install_method": proxy_method,
            "runner_latest_version": _string(proxy.get("latest_version")),
            "runner_update_status": _status(proxy.get("status")),
            "runner_update_checked_at": checked_at,
        },
    )
