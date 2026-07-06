from __future__ import annotations

from mcp import types
from mcp.shared.message import SessionMessage

from lightnow_proxy.capture import describe_session_message, initialize_client_context


def test_capture_describes_initialize_client_info() -> None:
    message = types.JSONRPCMessage.model_validate(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "codex", "version": "0.42.0"},
                "capabilities": {"roots": {}, "sampling": {}},
            },
        }
    )

    captured = describe_session_message(SessionMessage(message))

    assert captured is not None
    assert "request initialize" in captured
    assert "protocol_version: 2025-06-18" in captured
    assert "client_name: codex" in captured
    assert "client_version: 0.42.0" in captured
    assert "capability_keys: roots, sampling" in captured


def test_capture_extracts_initialize_client_context() -> None:
    message = types.JSONRPCMessage.model_validate(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "claude-desktop", "version": "0.12.3"},
                "capabilities": {"roots": {}, "sampling": {}},
            },
        }
    )

    context = initialize_client_context(SessionMessage(message))

    assert context == {
        "name": "claude-desktop",
        "version": "0.12.3",
        "capability_keys": ["roots", "sampling"],
    }


def test_capture_describes_tool_call_without_leaking_secrets() -> None:
    message = types.JSONRPCMessage.model_validate(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "jenkins__getBuild",
                "arguments": {
                    "jobFullName": "lightnow-ai/registry-ui/master",
                    "apiToken": "must-not-leak",
                },
                "_meta": {"requestId": "req-1"},
            },
        }
    )

    captured = describe_session_message(SessionMessage(message))

    assert captured is not None
    assert "request tools/call" in captured
    assert "tool_name: jenkins__getBuild" in captured
    assert "argument_keys: apiToken, jobFullName" in captured
    assert "meta_keys: requestId" in captured
    assert "must-not-leak" not in captured
    assert '"apiToken": "[REDACTED]"' in captured
