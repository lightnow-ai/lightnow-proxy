from __future__ import annotations

import json
from pathlib import Path

from lightnow_proxy import __version__


ROOT = Path(__file__).parents[1]


def test_registry_listing_uses_the_published_uvx_entry_point() -> None:
    listing = json.loads((ROOT / "server.json").read_text())
    package = listing["packages"][0]

    assert listing["name"] == "io.github.lightnow-ai/lightnow-proxy"
    assert listing["version"] == __version__
    assert package["registryType"] == "pypi"
    assert package["identifier"] == "lightnow-proxy"
    assert package["version"] == __version__
    assert package["runtimeHint"] == "uvx"
    assert package["transport"] == {"type": "stdio"}
