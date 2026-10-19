from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mcp.types import Tool
import pytest

import lightnow_proxy.upstream as upstream
from lightnow_proxy.upstream import ToolDiscoveryError, UpstreamMCPClient


class PagedClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def list_tools(self, *, cursor=None, cache_mode=None):
        self.calls.append((cursor, cache_mode))
        return self.pages[cursor]


def page(names, cursor=None):
    return SimpleNamespace(tools=[Tool(name=name, inputSchema={}) for name in names], next_cursor=cursor)


@pytest.mark.asyncio
async def test_discovery_reads_every_fresh_page_and_keeps_exact_names():
    client = PagedClient({None: page(["0", "00"], "next"), "next": page([" read "])})
    inventory = await UpstreamMCPClient().discover_tools(client)
    assert [tool.name for tool in inventory.tools] == ["0", "00", " read "]
    assert inventory.page_count == 2
    assert inventory.duration_ms >= 0
    assert client.calls == [(None, "bypass"), ("next", "bypass")]


@pytest.mark.asyncio
async def test_complete_empty_discovery_is_distinct_from_failure():
    inventory = await UpstreamMCPClient().discover_tools(PagedClient({None: page([])}))
    assert inventory.tools == []
    assert inventory.page_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("pages", [
    {None: page(["a"], "next"), "next": page(["b"], "next")},
    {None: page(["a"], "next"), "next": page(["a"])},
])
async def test_ambiguous_or_repeated_pages_never_return_partial_success(pages):
    with pytest.raises(ToolDiscoveryError, match="INVALID_TOOL_MANIFEST"):
        await UpstreamMCPClient().discover_tools(PagedClient(pages))


@pytest.mark.asyncio
async def test_discovery_enforces_page_and_tool_limits(monkeypatch):
    monkeypatch.setattr(upstream, "DISCOVERY_MAX_PAGES", 1)
    with pytest.raises(ToolDiscoveryError, match="DISCOVERY_LIMIT_EXCEEDED"):
        await UpstreamMCPClient().discover_tools(PagedClient({None: page(["a"], "more")}))
    monkeypatch.setattr(upstream, "DISCOVERY_MAX_TOOLS", 1)
    with pytest.raises(ToolDiscoveryError, match="DISCOVERY_LIMIT_EXCEEDED"):
        await UpstreamMCPClient().discover_tools(PagedClient({None: page(["a", "b"])}))


@pytest.mark.asyncio
async def test_discovery_bounds_cursor_and_definition_bytes(monkeypatch):
    monkeypatch.setattr(upstream, "DISCOVERY_MAX_CURSOR_BYTES", 2)
    with pytest.raises(ToolDiscoveryError, match="DISCOVERY_LIMIT_EXCEEDED"):
        await UpstreamMCPClient().discover_tools(PagedClient({None: page(["a"], "long")}))
    monkeypatch.setattr(upstream, "DISCOVERY_MAX_BYTES", 2)
    with pytest.raises(ToolDiscoveryError, match="DISCOVERY_LIMIT_EXCEEDED"):
        await UpstreamMCPClient().discover_tools(PagedClient({None: page(["a"])}))


@pytest.mark.asyncio
async def test_discovery_times_out_without_exposing_partial_results(monkeypatch):
    class SlowClient:
        async def list_tools(self, **kwargs):
            await asyncio.sleep(1)

    monkeypatch.setattr(upstream, "DISCOVERY_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(ToolDiscoveryError, match="DISCOVERY_TIMED_OUT"):
        await UpstreamMCPClient().discover_tools(SlowClient())


@pytest.mark.asyncio
async def test_definition_processing_cannot_finish_after_the_inventory_deadline(monkeypatch):
    clock = iter([1.0, 32.0])
    monkeypatch.setattr(upstream, "monotonic", lambda: next(clock))
    with pytest.raises(ToolDiscoveryError, match="DISCOVERY_TIMED_OUT"):
        await UpstreamMCPClient().discover_tools(PagedClient({None: page(["a"])}))
