# LightNow MCP Proxy

<!-- mcp-name: io.github.lightnow-ai/lightnow-proxy -->

[![PyPI](https://img.shields.io/pypi/v/lightnow-proxy.svg)](https://pypi.org/project/lightnow-proxy/)
[![Official MCP Registry](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fregistry.modelcontextprotocol.io%2Fv0.1%2Fservers%2Fio.github.lightnow-ai%252Flightnow-proxy%2Fversions%2Flatest&query=%24.server.version&label=Official%20MCP%20Registry&logo=modelcontextprotocol)](https://registry.modelcontextprotocol.io/?q=io.github.lightnow-ai%2Flightnow-proxy)
[![Listed on LightNow](https://lightnow.ai/badge/io.github.lightnow-ai/lightnow-proxy?variant=listed)](https://lightnow.ai/servers/io.github.lightnow-ai/lightnow-proxy)

Connect your AI clients to the MCP servers in your LightNow profiles.

The proxy runs on your computer. Your AI client connects to it, and the proxy
routes requests to the local or remote MCP servers you configure in LightNow.
You manage server connections and credentials in LightNow instead of maintaining
a separate server configuration in each client.

The **LightNow CLI** signs you in and configures your client. The **LightNow
Proxy** runs the connection whenever the client needs MCP tools.

## Get started

You need a [LightNow account](https://lightnow.ai/), an MCP client, and a Runtime profile containing the
servers you want to use. Local servers also need their own runtimes, such as
Node.js, Python or Docker, installed on your computer.

### 1. Install the CLI and proxy

With [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python
3.11 or later:

```sh
uv tool install lightnow-cli
uv tool install lightnow-proxy
```

Alternatively, install both with Homebrew:

```sh
brew tap lightnow-ai/tap
brew install lightnow-cli lightnow-proxy
```

Or with [pipx](https://pipx.pypa.io/stable/installation/):

```sh
pipx install lightnow-cli
pipx install lightnow-proxy
```

### 2. Sign in and connect a client

If your client already has MCP servers,
[import the configuration](https://docs.lightnow.ai/how-to/import-existing-mcp-config)
into a LightNow profile before syncing. Review the import preview to choose
which connections to keep.

```sh
lightnow login
lightnow sync --client codex --local-proxy --profile default
```

Use your profile's name in place of `default` if you have created a different
profile. The CLI writes the client entry and its proxy configuration. Restart
the client after syncing, then check its MCP tools list.

The CLI provides Local Proxy configuration for these clients:

| Client | `--client` value |
| --- | --- |
| Codex | `codex` |
| Claude Code | `claude-code` |
| Claude Desktop | `claude-desktop` |
| Cursor | `cursor` |
| VS Code | `vscode` |
| Google Antigravity | `antigravity` |
| Gemini CLI | `gemini-cli` |

For example, to configure Claude Code:

```sh
lightnow sync --client claude-code --local-proxy --profile default
```

See the [client setup guide](https://docs.lightnow.ai/getting-started/sync-mcp-clients)
for client-specific instructions and configuration paths.

### 3. Check the connection

For the Codex connection created above:

```sh
lightnow config-status --client codex
lightnow-proxy --config ~/.lightnow/lightnow-proxy/codex.yaml --health
```

`config-status` checks the client setup. `--health` resolves the profile and
contacts its servers to check tool discovery. It reports `healthy`, `degraded`
or `failed`, with details for each server. Add `--json` for machine-readable
output.

## Use profiles and organization connections

Choose which servers a client can use through its Runtime profile in LightNow.
The available tools come from those servers; the proxy includes no built-in
business tools. It supports local stdio servers and remote Streamable HTTP
servers, as well as resources exposed by those servers.

To add an organization connection, replace the organization ID and profile name
with your own values:

```sh
lightnow sync --client codex --local-proxy \
  --connection lightnow-team --tenant <organization-id> --profile engineering
```

Sign in with the intended account before syncing. Give each connection a distinct
`lightnow-…` alias when connecting several accounts or organizations to the same
client. Existing connections stay bound to the account used to create them.

See [Runtime profiles](https://docs.lightnow.ai/getting-started/integrations-and-runtime-profiles)
for server selection, credentials and organization access.

## Activity and privacy

LightNow can show client presence, connection health and MCP request activity in
the Control Plane. Runtime events include information such as the client,
profile, server, tool name, outcome and duration.

Optional activity capture and tool-argument capture have separate settings.
Argument capture is enabled by default; credential-like fields are redacted.
Managed connection events omit argument values. Tool results, resource contents
and resolved credentials are excluded from runtime events.

Managed connections also record the execution metadata required for their audit
history, independently of optional activity capture. Optional activity reporting
is best effort, so a missing event does not establish whether a tool ran.

See [Inspect runtime events](https://docs.lightnow.ai/how-to/inspect-runtime-events)
for the available views and capture settings, and
[Architecture](https://github.com/lightnow-ai/lightnow-proxy/blob/main/docs/ARCHITECTURE.md) for the request and data flows.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| The client cannot start `lightnow-proxy` | Confirm the proxy is installed and available on `PATH`, then sync again and restart the client. |
| Tools are missing | Check the selected profile, organization access and tool selection in LightNow. Run `--health` against that connection's generated YAML. |
| A local server cannot start | Check that its executable, runtime and required credentials are available on this computer. |
| The connection uses an unexpected account | Check which account owns the connection and sync it again under the intended account. |

Named connections have separate proxy files. For the `lightnow-team` example,
use `~/.lightnow/lightnow-proxy/codex-lightnow-team.yaml` with `--config`.

For configuration help, see
[Manage runtime secrets and config](https://docs.lightnow.ai/how-to/manage-runtime-secrets-and-config).

## Updates

```sh
lightnow update --check
lightnow update
```

The CLI supports updates for Homebrew, pipx and uv installations. Restart your
MCP client after updating the proxy.

## Contributing

Use Python 3.11 or later and uv to work on the source:

```sh
uv sync --locked --extra dev
uv run --locked --extra dev ruff check src tests examples
uv run --locked --extra dev pytest -q
```

See the [contribution guidelines](https://github.com/lightnow-ai/.github/blob/main/CONTRIBUTING.md)
and [release guide](https://github.com/lightnow-ai/lightnow-proxy/blob/main/docs/release.md).

## License

[Apache License 2.0](https://github.com/lightnow-ai/lightnow-proxy/blob/main/LICENSE).
