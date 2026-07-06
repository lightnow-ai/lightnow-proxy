# LightNow Local Proxy

[![PyPI](https://img.shields.io/pypi/v/lightnow-proxy.svg)](https://pypi.org/project/lightnow-proxy/)

The **LightNow Local Proxy** lets local MCP clients use one LightNow-managed
MCP entry instead of storing every MCP server configuration and secret in every
client.

It runs on your machine, uses your LightNow CLI login session, resolves the MCP
servers enabled for your selected LightNow profile, and forwards tool/resource
requests to local `stdio` or reachable Streamable HTTP MCP servers.

## Install

Requirements:

- Python 3.11 or higher
- `pipx`
- a [LightNow account](https://www.lightnow.ai/)
- the LightNow CLI

```sh
pipx install lightnow-cli
lightnow login
```

Install the proxy with `pipx`:

```sh
pipx install lightnow-proxy
```

Or with `uv`:

```sh
uv tool install lightnow-proxy
```

The Python package installs the `lightnow-proxy` command used by MCP clients.

For repository-local development:

```sh
uv tool install --from . lightnow-proxy
```

## Configure a Client

Use the LightNow CLI. It writes the client MCP entry and the per-client Local
Proxy config.

```sh
lightnow sync --client codex --local-proxy
lightnow sync --client claude-desktop --local-proxy
lightnow sync --client cursor --local-proxy
lightnow sync --client vscode --local-proxy
lightnow sync --client antigravity --local-proxy
```

Restart the MCP client after syncing.

Check the local setup:

```sh
lightnow config-status --client codex
```

## More Documentation

Detailed setup guides, examples, diagrams, supported client paths, telemetry
behavior and troubleshooting live in the LightNow docs:

- [Connect MCP clients](https://docs.lightnow.ai/getting-started/sync-mcp-clients)
- [CLI reference](https://docs.lightnow.ai/reference/cli)
- [Release process](docs/release.md)

## Local Development

For contributors working on this repository:

```sh
uv venv
uv pip install -e .[dev]
make test
```

Run the proxy with the example config:

```sh
LIGHTNOW_PROXY_CONFIG=config.example.yaml uv run lightnow-proxy
```

Run the proxy as a stdio MCP server:

```sh
uv run lightnow-proxy --config config.example.yaml --transport stdio
```
