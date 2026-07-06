from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Callable

import anyio
from mcp.shared.message import SessionMessage
from mcp import types

SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "password",
    "secret",
    "client_secret",
)


def capture_enabled(path: str | None) -> bool:
    return bool(path and path.strip())


def initialize_capture_file(path: str, config_path: str) -> None:
    capture_path = Path(path).expanduser()
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    with capture_path.open("a", encoding="utf-8") as handle:
        handle.write("LightNow Local Proxy MCP Protocol Capture\n")
        handle.write("=========================================\n")
        handle.write(f"started_at: {timestamp()}\n")
        handle.write(f"config: {config_path}\n")
        handle.write("redaction: secret-like keys are replaced with [REDACTED]\n\n")


SessionMessageObserver = Callable[[SessionMessage], None]


async def capture_read_stream(
    read_stream,
    capture_path: str | None = None,
    on_session_message: SessionMessageObserver | None = None,
):
    sender, receiver = anyio.create_memory_object_stream(0)

    async def forward_messages() -> None:
        async with sender:
            async for item in read_stream:
                if isinstance(item, SessionMessage):
                    if on_session_message is not None:
                        on_session_message(item)
                    if capture_path:
                        await write_capture(capture_path, describe_session_message(item))
                await sender.send(item)

    return receiver, forward_messages


async def write_capture(capture_path: str, block: str | None) -> None:
    if not block:
        return

    def append() -> None:
        with Path(capture_path).expanduser().open("a", encoding="utf-8") as handle:
            handle.write(block)
            handle.write("\n")

    await anyio.to_thread.run_sync(append)


def describe_session_message(session_message: SessionMessage) -> str | None:
    message = session_message.message.root
    if isinstance(message, types.JSONRPCRequest):
        return describe_request(message)
    if isinstance(message, types.JSONRPCNotification):
        return describe_notification(message)
    return None


def initialize_client_context(session_message: SessionMessage) -> dict[str, Any] | None:
    message = session_message.message.root
    if not isinstance(message, types.JSONRPCRequest):
        return None

    payload = message.model_dump(mode="json", by_alias=True, exclude_none=True)
    if payload.get("method") != "initialize":
        return None

    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    client_info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
    capabilities = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
    return {
        "name": client_info.get("name") if isinstance(client_info.get("name"), str) else None,
        "version": client_info.get("version") if isinstance(client_info.get("version"), str) else None,
        "capability_keys": sorted(str(key) for key in capabilities.keys()),
    }


def describe_request(message: types.JSONRPCRequest) -> str:
    payload = message.model_dump(mode="json", by_alias=True, exclude_none=True)
    method = str(payload.get("method") or "")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    redacted_params = redact(params)
    lines = [
        f"[{timestamp()}] request {method}",
        f"id: {payload.get('id')}",
        f"params_bytes: {json_size(params)}",
    ]

    if method == "initialize":
        lines.extend(describe_initialize_params(redacted_params))
    elif method == "tools/call":
        lines.extend(describe_tool_call_params(redacted_params, params))
    elif method in {"resources/read", "resources/list", "resources/templates/list", "tools/list"}:
        lines.append("params:")
        lines.append(pretty(redacted_params))
    else:
        lines.append("params:")
        lines.append(pretty(redacted_params))

    meta = redacted_params.get("_meta") if isinstance(redacted_params, dict) else None
    if isinstance(meta, dict):
        lines.append(f"meta_keys: {', '.join(sorted(meta.keys())) or '-'}")

    return "\n".join(lines) + "\n"


def describe_notification(message: types.JSONRPCNotification) -> str:
    payload = message.model_dump(mode="json", by_alias=True, exclude_none=True)
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    return "\n".join(
        [
            f"[{timestamp()}] notification {payload.get('method')}",
            f"params_bytes: {json_size(params)}",
            "params:",
            pretty(redact(params)),
            "",
        ]
    )


def describe_initialize_params(params: dict[str, Any]) -> list[str]:
    client_info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
    capabilities = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else {}
    return [
        f"protocol_version: {params.get('protocolVersion') or '-'}",
        f"client_name: {client_info.get('name') or '-'}",
        f"client_version: {client_info.get('version') or '-'}",
        f"capability_keys: {', '.join(sorted(capabilities.keys())) or '-'}",
        "params:",
        pretty(params),
    ]


def describe_tool_call_params(params: dict[str, Any], raw_params: dict[str, Any]) -> list[str]:
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    raw_arguments = raw_params.get("arguments") if isinstance(raw_params.get("arguments"), dict) else {}
    return [
        f"tool_name: {params.get('name') or '-'}",
        f"argument_keys: {', '.join(sorted(arguments.keys())) or '-'}",
        f"argument_bytes: {json_size(raw_arguments)}",
        "params:",
        pretty(params),
    ]


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = redact(nested)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def json_size(value: Any) -> int:
    return len(json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def timestamp() -> str:
    return datetime.now(UTC).isoformat()
