from __future__ import annotations

import json
from pathlib import Path

from lightnow_proxy import __version__


ROOT = Path(__file__).parents[1]


def load_listing() -> dict:
    return json.loads((ROOT / "server.json").read_text())


def test_registry_listing_matches_package_release() -> None:
    listing = load_listing()
    package = listing["packages"][0]

    assert listing["name"] == "io.github.lightnow-ai/lightnow-proxy"
    assert listing["version"] == __version__
    assert listing["repository"] == {
        "url": "https://github.com/lightnow-ai/lightnow-proxy",
        "source": "github",
        "id": "1291018077",
    }
    assert listing["websiteUrl"] == "https://docs.lightnow.ai/getting-started/sync-mcp-clients/"
    assert package["registryType"] == "pypi"
    assert package["registryBaseUrl"] == "https://pypi.org"
    assert package["identifier"] == "lightnow-proxy"
    assert package["version"] == __version__
    assert package["runtimeHint"] == "uvx"
    assert package["transport"] == {"type": "stdio"}


def test_registry_listing_requires_a_real_lightnow_profile() -> None:
    listing = load_listing()
    package_arguments = listing["packages"][0]["packageArguments"]
    config_argument = next(argument for argument in package_arguments if argument["name"] == "--config")
    transport_argument = next(argument for argument in package_arguments if argument["name"] == "--transport")

    assert config_argument["isRequired"] is True
    assert config_argument["format"] == "filepath"
    assert config_argument["placeholder"] == "~/.lightnow/lightnow-proxy/default.yaml"
    assert "lightnow sync" in config_argument["description"]
    assert transport_argument["value"] == "stdio"
    assert [argument["name"] for argument in package_arguments] == ["--config", "--transport"]


def test_registry_listing_is_human_facing_and_contains_no_fixture_tools() -> None:
    listing = load_listing()
    serialized = json.dumps(listing).lower()

    assert listing["title"] == "LightNow Secure MCP Gateway"
    assert listing["description"] == (
        "Securely connect AI clients to your team's approved MCP servers—no copied configs or secrets."
    )
    assert 1 <= len(listing["description"]) <= 100
    assert "echo__echo" not in serialized
    assert "synthetic" not in serialized
