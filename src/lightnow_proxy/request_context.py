from __future__ import annotations

from contextvars import ContextVar

from lightnow_proxy.auth import Principal


current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)


def get_current_principal() -> Principal | None:
    return current_principal.get()
