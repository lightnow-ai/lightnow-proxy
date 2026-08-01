from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from lightnow_proxy.auth import Principal


current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)
current_mcp_client_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "current_mcp_client_context",
    default=None,
)


def get_current_principal() -> Principal | None:
    return current_principal.get()


def get_current_mcp_client_context() -> dict[str, Any] | None:
    return current_mcp_client_context.get()
