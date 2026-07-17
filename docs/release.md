# Release Process

## Release Stage

`lightnow-proxy` follows the same public release model as `lightnow-cli`.
Prepare a clean public repository first, then publish the Python package and
Homebrew formula from that repository.

## Public Package Metadata

Use these settings for the public package:

- Repository name: `lightnow-proxy`
- PyPI project name: `lightnow-proxy`
- Official MCP Registry name: `io.github.lightnow-ai/lightnow-proxy`
- Installed command: `lightnow-proxy`
- License: `Apache-2.0`
- Topics: `lightnow`, `mcp`, `model-context-protocol`, `local-proxy`

Keep the installed command as `lightnow-proxy` so generated MCP client configs
use a LightNow-owned executable name and do not collide with generic MCP proxy
packages.

## Official MCP Registry

The official MCP Registry listing is prepared from `server.json`. The Registry
hosts metadata only; the runnable artifact remains the PyPI package.

For PyPI ownership verification, keep this marker in `README.md` so it is
present in the PyPI long description:

```markdown
<!-- mcp-name: io.github.lightnow-ai/lightnow-proxy -->
```

Before publishing the Registry listing:

1. Ensure `server.json` version and package version match `pyproject.toml`.
2. Ensure the referenced PyPI version is already published.
3. Install the official publisher:
   ```bash
   brew install mcp-publisher
   ```
4. Authenticate with the Registry:
   ```bash
   mcp-publisher login github
   ```
5. Publish from the repository root:
   ```bash
   mcp-publisher publish
   ```
6. Verify the published metadata:
   ```bash
   curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.lightnow-ai/lightnow-proxy"
   ```

## Checklist

Release Please owns normal version bumps, changelog updates, Git tags, and
GitHub Releases from Conventional Commits.

Required GitHub setup:

- Repository secret: `RELEASE_PLEASE_TOKEN`
- Token permissions: contents, pull requests, and issues read/write for this
  repository
- Repository secret: `HOMEBREW_TAP_TOKEN`
- Token permissions: contents read/write for `lightnow-ai/homebrew-tap`; the
  token is used only to send the post-PyPI repository dispatch
- Repository Actions setting: allow GitHub Actions to create pull requests

Use a real PAT or GitHub App token for `RELEASE_PLEASE_TOKEN`, not the default
`GITHUB_TOKEN`. Tags created with the default token do not trigger the existing
tag-based `Release` workflow, so PyPI publishing would not run.

Normal release flow:

1. Merge Conventional Commit changes to `main`.
2. Release Please opens or updates a release PR.
3. Merge the release PR when ready.
4. Release Please creates the `vX.Y.Z` tag and GitHub Release.
5. The existing tag-based `Release` workflow builds and publishes PyPI
   distributions through Trusted Publishing.
6. After PyPI succeeds, the release workflow dispatches the exact published
   version to `lightnow-ai/homebrew-tap`. The tap regenerates, audits, installs,
   and tests the formula before committing it.
7. Publish or update the official MCP Registry listing from `server.json`.

Manual release checklist for fallback or first-time setup:

1. Update `pyproject.toml` to the target semantic version.
2. Run:
   ```bash
   make lint
   make test
   rm -rf dist
   uv run --with build --with twine python -m build
   uv run --with twine python -m twine check dist/*
   ```
3. Verify the GitHub Actions `CI` workflow is green on `main`.
4. Configure PyPI Trusted Publishing before the first PyPI release:
   - PyPI project name: `lightnow-proxy`
   - Owner: `lightnow-ai`
   - Repository: `lightnow-proxy`
   - Workflow: `release.yml`
   - Environment: `pypi`
5. Create a signed or otherwise traceable Git tag:
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
6. Verify the GitHub Actions `Release` workflow:
   - tests pass,
   - source distribution and wheel are built,
   - `twine check` passes,
   - a GitHub Release is created with generated release notes and distribution artifacts,
   - PyPI publish succeeds through Trusted Publishing.
7. Verify the `Update formula` workflow in `lightnow-ai/homebrew-tap`. For a
   recovery run, dispatch it manually with formula `lightnow-proxy` and the
   exact already-published version; it uses the same generation and verification
   path as the automatic release dispatch.
8. Publish or update the official MCP Registry listing from `server.json`.
9. Verify fresh installs on a clean machine or isolated tool environment:
   ```bash
   uv tool install lightnow-proxy
   lightnow-proxy --version
   pipx install lightnow-proxy
   lightnow-proxy --version
   ```
10. Verify one local active upstream check:
   ```bash
   lightnow-proxy --health --json
   ```
11. Verify at least one real Local Proxy sync and MCP call with the current
    LightNow CLI against the local or production Registry API.

Do not publish with long-lived local PyPI tokens.
