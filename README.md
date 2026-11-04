# LightNow MCP Proxy

<!-- mcp-name: io.github.lightnow-ai/lightnow-proxy -->

[![PyPI](https://img.shields.io/pypi/v/lightnow-proxy.svg)](https://pypi.org/project/lightnow-proxy/)
[![Official MCP Registry](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fregistry.modelcontextprotocol.io%2Fv0.1%2Fservers%2Fio.github.lightnow-ai%252Flightnow-proxy%2Fversions%2Flatest&query=%24.server.version&label=Official%20MCP%20Registry&logo=modelcontextprotocol)](https://registry.modelcontextprotocol.io/?q=io.github.lightnow-ai%2Flightnow-proxy)
[![Listed on LightNow](https://lightnow.ai/badge/io.github.lightnow-ai/lightnow-proxy?variant=listed)](https://lightnow.ai/servers/io.github.lightnow-ai/lightnow-proxy)

**Connect your AI clients to your MCP servers—securely managed in one place.**

The LightNow MCP Proxy is the local runtime behind LightNow profiles.
Connect Codex, Claude Desktop, Cursor, VS Code, or Antigravity once, then manage
the MCP servers available to that client in LightNow instead of copying server
configuration and secrets into every tool.

- Keep MCP access organized in personal and organization profiles.
- Connect local `stdio` and remote Streamable HTTP servers through one entry.
- Resolve credentials on the user's machine instead of embedding them in MCP
  client configuration.
- Apply profile and policy changes without rebuilding every client setup.
- Observe client, profile, server health, and tool usage. Tool arguments are
  captured by default with credential-like fields redacted and can be disabled;
  tool results and resolved secrets are never collected.

The installed command remains `lightnow-proxy`. It runs locally, uses the
identity-bound LightNow CLI session, resolves the selected profile, and routes
tool and resource requests to the profile's MCP servers.

## Capabilities come from your LightNow profile

The proxy deliberately does not ship demo tools. Its MCP capabilities are
the real tools and resources exposed by the servers selected in the active
LightNow profile, so the list differs between teams and clients. Names such as
`github__create_issue` identify both the upstream server and its tool and avoid
collisions when several servers use the same tool name.

The proxy supports the official MCP `2026-07-28` revision with stateless,
per-request protocol metadata and `server/discover`. It keeps automatic
compatibility with handshake-era servers and clients through `2025-11-25`.

Tool discovery reads complete pagination from the actual configured upstream.
It fails explicitly on repeated cursors, duplicate exact names or limits of
30 seconds, 100 pages, 2,000 tools and 4 MiB of definitions. A complete empty
inventory stays distinct from incomplete discovery. Modern HTTP calls prepare
their parameter-header schemas from that same fully discovered client.
The managed dispatch primitive authorizes that inventory before sending once;
schema-mismatch recovery and interactive retries must return to its policy gate.

When Registry API requests an optional sanitized setup preview, the Proxy projects
only public input IDs, binding booleans, typed placeholders and explicitly allowed
public choice indexes from the native managed context. It does not parse launch
commands or copy configuration values, files, endpoints or tool metadata into the
preview. Unsupported or mismatched descriptors produce an unavailable preview
without failing successful private discovery. Sharing remains a separate explicit
API/UI action; this Proxy does not create or publish contributions. The full
workflow is specified in [Registry API #1340](https://github.com/lightnow-ai/registry-api/pull/1340).

For managed connections the existing device heartbeat declares
`managed-connections.v1` and obtains the actor-owned runtime client ID, even when
optional activity capture is disabled.
Compatible synchronized proxies poll every 15 seconds and execute at most two
queued private discoveries concurrently. They resolve the exact current assignment,
compare queued profile, policy and credential revisions before opening the upstream,
and report to the original request with If-Match. Shutdown cancels outstanding
discovery work; failures report bounded codes without private definitions or raw
upstream errors. Legacy presence-only clients retain the 120-second interval.
Registry supplies the canonical connection, assignment and bounded bootstrap context. Complete private inventory reporting
returns exact allowed original names from the same backend authorizer as dispatch.
The Proxy filters discovery and checks that decision again on the actual upstream
session before one business call. Expected managed denials return a normal MCP tool
error containing a content-free reason code. Absolute expiry and a monotonic deadline both
apply; reporting does not renew the lease. Managed contexts do not authorize
resource methods. Synchronized profiles bypass persistent tool caches and stale
fallbacks. Canonical aliases take precedence; required organization admission also
excludes local static and legacy runtime overrides from the managed profile.
Managed wire names use `ln_<full assignment UUID hex>__<original tool name>`.
Descriptions and metadata retain readable aliases. Aliases containing underscores,
renames, and replacement assignments therefore cannot transfer tool identity.
The reserved UUID namespace cannot fall back to static/legacy routing after an
assignment is removed; ordinary legacy tool names retain their existing form.

Managed calls also retain content-free required evidence independently of optional
activity capture. A private SQLite journal beside the CLI session commits each
attempt before startup and handoff intent before dispatch. It retries only the
original event UUIDs, subject and reporting receipt; it never retries a business
call. Native OS locks distinguish live calls from interrupted ones. Lost outcomes
stay unknown. The Registry's explicit `RUNTIME_PROFILE_FORBIDDEN` response records
a denied call using its original reporting receipt; it is not an infrastructure
outage. Unavailable storage or exhausted capacity blocks dispatch. Local
`/status` exposes pending/unresolved counts and closed delivery codes. The bounded
journal retains rejected/expired facts for explicit resolution. See
[the evidence contract](docs/ARCHITECTURE.md#required-managed-call-evidence).




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

Version-pinned KV2 references are read using their exact Vault version. The
reader checks the returned version, rejects deleted/destroyed versions and, for
LightNow write operations, verifies the operation identifier before injecting a
value. The installed reader declares `secret-store.versioned-read.v1` in the
existing device registration and runtime header independently of any writer.
A missing or mismatched version
fails the request; it never falls back to the latest Vault value.

An enrolled organization connector authenticates with its own OAuth client and
local RSA private key using standard `private_key_jwt`. Its Registry connection
uses `client_private_key_file`, `client_key_id` and `client_scope` alongside the
enrollment's `client_id` and `token_url`. Protect the key file with the operating
system's credential-file permissions; only its public JWK belongs in enrollment.
This mode cannot share a personal CLI session or fall back to a client secret.
Machine tokens renew before expiry, and replacing the local key invalidates the
cached token. These authentication settings alone do not enroll or authorize a
connector; the server must separately admit its current enrollment and provider.

### Opt-in private Secret Store worker

The worker is disabled by default. Its journal requires native POSIX ownership
and a private `0700` directory; unsupported ownership validation fails configuration.
Existing nonprivate directories are rejected without changing their permissions.
Enable it only on the enrolled executor with
an explicit local provider mapping. It executes server-issued KV2 writes and
exact-version readbacks; it never invents a Vault path or migrates existing
secrets. Registry authorization, provider/Store policy and current enrollment
remain required on every claim and result.

For a personal proxy, keep the normal identity-bound generated CLI configuration
(`registry_api.use_cli_session`, `expected_issuer`, `expected_subject` and observed
`local_proxy` device/client IDs). It must use the personal context, with no tenant
in either configuration or the current CLI session. Add:

```yaml
runtime_secrets:
  worker_enabled: true
  journal_path: ~/.lightnow/secret-store/operations.sqlite3
  poll_interval_seconds: 5
  providers:
    11111111-1111-4111-8111-111111111111: # Replace with the enrolled provider ID.
      auth_mode: vault-proxy
      vault_proxy_url: http://127.0.0.1:8200
```

Start the regular MCP proxy:

```sh
lightnow-proxy --config ~/.lightnow/lightnow-proxy/default.yaml --transport stdio
```

Its existing heartbeat receives personal work and schedules the actual worker.
Only after the worker validates its fresh personal session does registration and
the runtime header include `secret-store.operations.v1`. Switching the CLI session
to an organization disables this personal writer; it does not become a tenant
writer. A normal tenant proxy can still read authorized versioned references
using the separate reader capability, without enabling the worker.

An organization connector uses its separately enrolled machine identity and an
explicit connector ID. It runs outbound only, without an MCP/HTTP listener:

```yaml
auth:
  enabled: false
  issuer: https://identity.example/realms/lightnow
registry_api:
  enabled: true
  base_url: https://registry-api.example/v0.1
  expected_issuer: https://identity.example/realms/lightnow
  token_url: https://identity.example/realms/lightnow/protocol/openid-connect/token
  client_id: enrolled-connector-client
  client_private_key_file: ~/.lightnow/secret-store/connector.pem
  client_key_id: enrolled-key-id
  client_scope: secret-store.operations.v1
local_proxy:
  enabled: false
runtime_secrets:
  worker_enabled: true
  connector_id: 22222222-2222-4222-8222-222222222222
  journal_path: ~/.lightnow/secret-store/connector.sqlite3
  poll_interval_seconds: 5
  providers:
    11111111-1111-4111-8111-111111111111:
      auth_mode: vault-proxy
      vault_proxy_url: http://127.0.0.1:8200
profiles:
  worker: {}
upstreams: {}
```

Replace the example origins/IDs with the exact enrollment values and protect the
configuration/key with mode `0600`. Use `registry_api.ca_file` for a private
Registry/identity CA. Provider mappings support the existing local Vault Proxy
and TLS/keyring settings; no bootstrap credential is delivered by Registry.

```sh
lightnow-proxy --config ~/.lightnow/secret-store/connector.yaml --secret-store-worker
```

Stop either process with Ctrl-C (or the owning service's normal TERM stop).
Restart with the same configuration and journal. Set `worker_enabled: false` to
stop automatic execution on the next startup. An organization connector cannot
start in the ordinary server mode and cannot borrow a personal token or client
secret. It processes only its current machine-authorized organization/provider
pool; reading the resulting MCP credential remains the normal tenant proxy's job.

The separate SQLite journal contains only identity/operation/claim IDs, phase and
expiry. It stores the request ID durably **before** claiming and records dispatch
**before** IO. A namespace lock serializes processes across claim/IO/result; the
namespace binds API, issuer, subject, OAuth client and exact runtime client or
connector. Files use `0600`, the parent directory `0700`. No values, value hashes
or claim payloads are journaled. Lost responses and restarts reuse the same
request ID and can only read back an already dispatched write. Unknown outcomes
and authorization errors retain the nonce; local clock/TTL expiry never permits
a new write. Only a confirmed terminal receipt or the authorized server response
`data: null` completes it. Terminal tombstones remain at least ten minutes;
the journal caps at 1,024 rows and stops new admissions when full. Do not delete
or reset a journal to retry uncertain work. Restore the original identity and
let the server's canonical expiry/receipt reconcile it instead.

### Managed runtime files

STDIO servers may attach bounded, non-secret `runtime_files` to their Runtime
Profile client configuration. LightNow Proxy writes each file set below
`~/.lightnow/runtime-files` in an immutable content-addressed directory before
the upstream process starts. Files use mode `0600`; managed directories use
mode `0700`.

Reference the resolved directory with `${LIGHTNOW_RUNTIME_DIR}` in arguments,
environment values, or the working directory:

```json
{
  "transport": "stdio",
  "command": "npx",
  "args": [
    "@bytebase/dbhub@latest",
    "--config",
    "${LIGHTNOW_RUNTIME_DIR}/dbhub.toml"
  ],
  "env": {
    "DBHUB_DSN": "${DBHUB_DSN}"
  },
  "runtime_files": [
    {
      "path": "dbhub.toml",
      "content": "[[sources]]\nid = \"analytics\"\ndsn = \"${DBHUB_DSN}\"\n"
    }
  ]
}
```

Runtime files are UTF-8 text and are limited to 16 files, 64 KiB per file, and
256 KiB per server configuration. Paths must be relative and cannot contain
traversal segments or backslashes. Secret values must remain in LightNow's
runtime secret buckets; files may refer to the corresponding environment names
but must not embed plaintext credentials. Invalid or modified materializations
fail closed before the upstream starts.

## More Documentation

Detailed setup guides, examples, diagrams, supported client paths, telemetry
behavior and troubleshooting live in the LightNow docs:

- [Connect MCP clients](https://docs.lightnow.ai/getting-started/sync-mcp-clients)
- [CLI reference](https://docs.lightnow.ai/reference/cli)
- [Release process](https://github.com/lightnow-ai/lightnow-proxy/blob/main/docs/release.md)

## Local Development

For contributors working on this repository:

```sh
uv venv
uv pip install -e .[dev]
make test
```

Jenkins builds the runtime image, runs the Ruff and pytest commands from
`make lint test` inside it, then checks `lightnow-proxy --help`.
The checkout is mounted read-only and test environments live in the container.
The `Jenkinsfile` uses the native
`build-agent` capability pool and removes only its own validation image. The timeout includes agent allocation.
GitHub Actions continues to run the Python version matrix. Before merging an
Ops dependency update, require terminal Jenkins success at the current PR head.
The pipeline validates changes without publishing an image or changing accounts.

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
