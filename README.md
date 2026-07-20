# LightNow Secure MCP Gateway

<!-- mcp-name: io.github.lightnow-ai/lightnow-proxy -->

[![PyPI](https://img.shields.io/pypi/v/lightnow-proxy.svg)](https://pypi.org/project/lightnow-proxy/)

**Securely connect AI clients to your team's approved MCP servers—no copied
configs or secrets.**

The LightNow Secure MCP Gateway is the local runtime behind LightNow profiles.
Connect Codex, Claude Desktop, Cursor, VS Code, or Antigravity once, then manage
the MCP servers available to that client in LightNow instead of copying server
configuration and secrets into every tool.

- Keep MCP access organized in personal and organization profiles.
- Connect local `stdio` and remote Streamable HTTP servers through one entry.
- Resolve credentials on the user's machine instead of embedding them in MCP
  client configuration.
- Apply profile and policy changes without rebuilding every client setup.
- Observe client, profile, server health, and tool usage without collecting tool
  results or resolved secrets.

The installed command remains `lightnow-proxy`. It runs locally, uses the
identity-bound LightNow CLI session, resolves the selected profile, and routes
tool and resource requests to the profile's MCP servers.

## Capabilities come from your LightNow profile

The gateway deliberately does not ship demo tools. Its MCP capabilities are
the real tools and resources exposed by the servers selected in the active
LightNow profile, so the list differs between teams and clients. Names such as
`github__create_issue` identify both the upstream server and its tool and avoid
collisions when several servers use the same tool name.

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

Install the proxy with Homebrew:

```sh
brew tap lightnow-ai/tap
brew install lightnow-proxy
```

Or install it with `pipx`:

```sh
pipx install lightnow-proxy
```

Or install it with `uv`:

```sh
uv tool install lightnow-proxy
```

The Python package installs the `lightnow-proxy` command used by MCP clients.

For repository-local development:

```sh
uv tool install --from . lightnow-proxy
```

Update supported CLI and Proxy installations through the LightNow CLI:

```sh
lightnow update --check
lightnow update
```

Homebrew, pipx and uv are managed. The proxy only reports its observed version
and update state in metadata-only heartbeats; it never invokes a package manager
or delays MCP startup to check for releases.

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

### Multiple accounts and organizations

Use a distinct `--connection` alias for every account, organization or profile
that should appear in the same MCP client:

```sh
lightnow login
lightnow sync --client codex --local-proxy \
  --connection lightnow-personal --profile default

# Sign in with the organization account before creating this connection.
lightnow login
lightnow sync --client codex --local-proxy \
  --connection lightnow-acme --tenant <tenant-id> --profile engineering
```

Each generated proxy config has a stable connection ID and points to one named
CLI session under `~/.lightnow/sessions/`. It also records the expected issuer
and subject. The proxy refuses Registry requests when those values do not
match, so a later CLI login cannot silently switch an existing connection. No
access or refresh token is written to the proxy YAML.

Check the local setup:

```sh
lightnow config-status --client codex
```

Check whether the proxy can resolve the selected profile and reach its upstream
MCP servers:

```sh
lightnow-proxy --health
lightnow-proxy --health --json
```

By default this reads `~/.lightnow/lightnow-proxy/default.yaml`, which is written
when the default profile is synced into Local Proxy mode. For a client-specific
config, pass the generated path explicitly:

```sh
lightnow-proxy --config ~/.lightnow/lightnow-proxy/codex.yaml --health
lightnow-proxy --config ~/.lightnow/lightnow-proxy/codex.yaml --health --json
```

Named connections use separate files such as
`~/.lightnow/lightnow-proxy/codex-lightnow-acme.yaml`. The JSON health report
shows their non-secret connection alias, ID, account label, scope, profile and
identity-binding status. Legacy configs that use `cli_config_path` remain
readable, but are restricted to the configured authentication issuer.

When telemetry is enabled, active health checks and runtime events are sent to
the LightNow Control Plane. Tool-call arguments are captured by default and can
be disabled independently in the Local Proxy settings. Credential-like fields
are redacted before transmission. The proxy also sends device
presence immediately at startup and every two minutes. The Control Plane can
then show which devices, clients and profiles are active, healthy, degraded, or
failing, along with CLI/Proxy versions and update status. Tool results, resolved
LightNow secrets, unredacted authorization values, network addresses, hardware
identifiers and local paths are not stored. Credential-like argument fields,
including authorization headers, are replaced with `[REDACTED]` before transmission.

Vault providers configured for runtime resolution are resolved on this host,
after Registry API has returned a provider reference without credentials or a
secret value. HashiCorp Vault Proxy auto-auth on `127.0.0.1:8200` is the
default. Provider-specific loopback listeners can be mapped under
`runtime_secrets.providers` in the generated YAML; LightNow CLI preserves these
non-secret mappings on subsequent syncs. The optional OS-keyring path is
available with `lightnow-proxy[keyring]`. Resolution failures are fail-closed
and plaintext values are never added to the tool-schema cache.

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
uv run lightnow-proxy --config config.example.yaml
```

Run the proxy as a stdio MCP server:

```sh
uv run lightnow-proxy --config config.example.yaml --transport stdio
```

Run a local health check against the example config:

```sh
uv run lightnow-proxy --config config.example.yaml --health
```
