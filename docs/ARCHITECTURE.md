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

The local MCP endpoint is unauthenticated because desktop MCP clients start it
as a local process. It therefore validates MCP HTTP `Host` and `Origin` headers
and allows only loopback browser origins. A non-loopback `server.public_url` is
ignored for Local Proxy browser-origin allowlisting; Local Proxy mode is not a
remote hosted endpoint.

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

When a CLI-generated Local Proxy configuration includes stable device and
client-instance UUIDs, the proxy also sends one presence heartbeat at startup
and every 120 seconds. The Registry API computes active presence with a
300-second threshold. Heartbeat failures are logged and exposed through local
status, but never block MCP traffic; the next regular heartbeat is the only
retry. Disabling runtime telemetry disables both events and presence.

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

## Status And Active Checks

`/status` is the canonical non-secret local status endpoint. By default it
returns static runtime posture: local proxy mode, selected profile, runner
identity, client identity, telemetry flag, observe/enforce policy posture,
unmanaged-server policy, Registry API usage flags, profile names and static
upstream names. It is meant for local installers and support tooling that need
to inspect the configured posture without touching upstream MCP servers.

`/status?check=upstreams` and `lightnow-proxy --health` perform an active
upstream check. They resolve the selected profile and run tool discovery against
each upstream MCP server. The report classifies the proxy as:

- `healthy`: the selected profile resolves and all upstreams answer.
- `degraded`: the proxy can run, but at least one upstream failed or the active
  profile has no upstreams.
- `failed`: the selected profile cannot be resolved.

The active health report includes profile name, upstream alias, transport,
server name, duration, tool count and redacted error type/message. It must not
include secrets, authorization headers, tool arguments, tool results or resource
contents.

The CLI exits with code `0` for `healthy`, `2` for `degraded`, and `1` for
`failed` so installers and future UI handoff code can distinguish "proxy is
down" from "one configured server is unhealthy".

## Legacy Profile Proxy

The repository previously exposed one remote MCP endpoint per profile with
Keycloak bearer-token checks. That shape remains available only when
`local_proxy.enabled` is false.

Profile endpoints also validate MCP HTTP `Host` and `Origin` headers. Unlike
Local Proxy mode, they may allow the configured `server.public_url` host because
they require bearer-token authentication before routing MCP requests.

`profiles.*.required_groups` match Keycloak group paths after trimming leading
and trailing slashes. Top-level paths such as `/lightnow-proxy-admins` therefore
match `lightnow-proxy-admins`, but nested groups are not reduced to their final
path segment. For example, `/team-a/admin` does not grant access to a profile
requiring `/team-b/admin` or plain `admin`.
