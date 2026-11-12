# How LightNow Proxy works

LightNow Proxy connects an AI client to the MCP servers in a selected LightNow
Runtime profile. It runs on the user's computer, where it can start local
servers and reach services available on that machine's network.

For installation and client configuration, start with the [README](../README.md).

## Components and request flow

```text
AI client
    │ MCP requests and responses
    ▼
LightNow Proxy on your computer
    ├── LightNow: profile, access and connection configuration
    ├── Local MCP servers: started over stdio
    └── Remote MCP servers: reached over Streamable HTTP
```

The LightNow CLI signs the user in and writes the AI client's connection entry.
With the usual generated configuration, the client starts the proxy as a local
stdio process. A separate background service or public HTTP listener is not
needed for that setup.

The proxy loads the selected profile, resolves the requested server connection
and forwards the MCP operation. Tool results and resource contents return to the
AI client through the proxy. Each server still applies its own authentication
and permissions.

## Profiles and access

A profile selects the server connections available to a client. Personal and
organization connections use the account and scope selected during CLI setup.
Several named connections can coexist in one AI client.

For managed connections, current organization access and the assignment's tool
selection determine which tools can be used. A tool appearing in an earlier
client menu does not preserve permission after access is withdrawn. The client
may need a refresh or restart to update its displayed tool list.

The proxy's tools come from the configured servers. It gives them distinct
names so that servers with the same tool name can be used together. Clients
should use the names returned by MCP discovery.

Tools and resources are supported. Other MCP capabilities depend on explicit
proxy support; connecting a server does not automatically make all of its
capabilities available through the proxy.

## Credentials and local configuration

The generated client entry starts the proxy with a connection-specific YAML
file. Server credentials are resolved when needed rather than copied into that
client entry. The proxy configuration refers to the local CLI session and stays
bound to the account that created the connection.

Local stdio servers run with the operating-system permissions of the proxy.
Their executables and runtimes must be installed on that computer. Remote
servers must be reachable from it.

A server can also use non-secret configuration files managed in its profile.
Credentials belong in runtime secrets or an authorized external secret store.
See [Manage runtime secrets and config](https://docs.lightnow.ai/how-to/manage-runtime-secrets-and-config)
for setup, including HashiCorp Vault and private-network resolution.

## What LightNow records

| Data | Where it goes |
| --- | --- |
| Tool requests and responses | Between the AI client, local proxy and selected MCP server |
| Profile and connection configuration | From LightNow to the authorized proxy |
| Optional runtime activity | To LightNow for request diagnostics and connection health |
| Required managed-call audit records | To LightNow for managed connection activity |

Optional activity can include tool names, outcomes, timings and client context.
Argument capture is configurable and enabled by default; credential-like fields
are redacted. Managed connection events omit argument values. Runtime events exclude
tool results, resource contents and resolved credentials.

Required managed-call audit records describe execution metadata, including
policy refusals and reported outcomes. They remain separate from optional
activity capture. An unknown outcome is not evidence that an action succeeded
or failed at the upstream service.

Optional activity delivery is best effort. Use the server's own records when
you need to establish the final business outcome of an operation.

## Checking a deployment

`lightnow config-status` checks the AI client's LightNow configuration.
`lightnow-proxy --config <generated-proxy.yaml> --health` checks profile
resolution and upstream tool discovery. Neither runs a business tool to prove
that a particular action will succeed.

In LightNow, **Control Plane → Requests** shows captured runtime activity.
A managed connection's **Activity** view shows its managed-call history.
See [Inspect runtime events](https://docs.lightnow.ai/how-to/inspect-runtime-events)
for filtering and interpreting those records.
