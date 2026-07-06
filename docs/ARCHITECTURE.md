# LightNow Local Proxy Architecture

## Goal

The LightNow Local Proxy is a local-only MCP proxy. Users configure one
LightNow entry in their MCP client. The proxy owns the selected MCP server
configuration and routes tool calls to the configured upstreams.

This lets LightNow move from "every client contains every MCP server config" to
"the client contains one local LightNow endpoint".

## Request Flow

```text
MCP client
  -> http://127.0.0.1:{port}/mcp
  -> LightNow Local Proxy
  -> active profile
  -> configured upstream MCP server
  -> prefixed tool/resource result
```

The Local Proxy exposes upstream tools as `{upstream}__{tool}`. Prefixing avoids
tool name collisions and gives later monitoring a stable upstream identity.
Resources are exposed as `lightnow://{upstream}/{encoded_original_uri}` so a
client can read them through the proxy without leaking resource contents into
LightNow analytics.

## Local Proxy Mode

When `local_proxy.enabled` is `true`, the app exposes only the configured local
MCP endpoint, for example `/mcp`. The legacy profile endpoint shape
`/profiles/{profile}/mcp` is not mounted in this mode.

```yaml
local_proxy:
  enabled: true
  profile: default
  path: /mcp
  client_name: codex
  runner_name: lightnow-local-proxy
  client_transport: stdio
```

The selected profile determines which upstreams are visible:

```yaml
profiles:
  default:
    upstreams:
      - echo
      - math
```

## Upstream Transports

The Local Proxy can route to both currently relevant MCP transport classes:

- `stdio`: launch a local MCP server process with fixed command, args, cwd, and
  environment.
- `streamable-http`: call a reachable MCP server over HTTP.

Both transports can sit behind the same local client-facing `/mcp` endpoint. In
practice, `stdio` upstreams are the strongest reason for a local proxy because a
hosted service cannot run customer-local binaries or reach customer-local files
without a separate enterprise deployment model.

## Configuration Source

Static YAML remains available for fixtures and local protocol tests. In normal
user flows, the CLI writes a per-client Local Proxy config and the proxy fetches
the active LightNow MCP configuration at runtime.

1. User enables Local Proxy Mode in LightNow.
2. The local installer or CLI writes one client MCP config entry.
3. The CLI writes a per-client Local Proxy config with the selected profile,
   client identity, telemetry flag and observe/enforce policy posture.
4. The Local Proxy fetches the active LightNow MCP config.
5. LightNow UI controls which upstreams are available.

## Runtime Telemetry

Runtime telemetry is metadata-only. The proxy emits events for:

- runner lifecycle start/stop
- `tools/list`
- `tools/call`
- `resources/list`
- `resources/read`
- `resources/templates/list`

Events identify both the MCP end-client (`mcp_client_name`, for example
`codex`) and the local process (`runner_name`, for example
`lightnow-local-proxy`). This distinction matters because the runner is the
execution boundary, but the customer needs to understand which MCP client is
using a profile.

Events may include method, status, duration, upstream transport, server alias,
tool name, resource URI, response item count, request/response byte counts,
MIME/content type and error type. Tool-call events may include top-level
argument key names, MCP request metadata keys and normalized client context.
Client-specific metadata is translated through adapters before it reaches
Registry API. Codex `x-codex-turn-metadata` maps to generic session, thread,
turn, model, reasoning and sandbox fields while still filling legacy `codex_*`
fields for compatibility. Antigravity `antigravity.google/conversation_id` maps
to the generic thread/conversation field. Local paths such as Antigravity
artifact directories are observed only as metadata keys and must not be stored
as values. Runtime telemetry must not include tool arguments, tool results,
resource contents, workspace paths, git remotes, commit hashes, secrets or
authorization headers.

For daemon-style local runs, `/status` returns a non-secret status document with
the local proxy mode, selected profile, runner identity, client identity,
telemetry flag, observe/enforce policy posture, unmanaged-server policy,
Registry API usage flags, profile names and static upstream names. It is meant
for local health checks and future UI/installer handoff.

## Legacy Profile Proxy

The repository previously exposed one remote MCP endpoint per profile with
Keycloak bearer-token checks. That shape remains available only when
`local_proxy.enabled` is false.
