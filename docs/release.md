# Release Process

## Release Stage

`lightnow-proxy` follows the same public release model as `lightnow-cli`.
Prepare a clean public repository first, then publish the Python package and
Homebrew formula from that repository.

## Public Package Metadata

Use these settings for the public package:

- Repository name: `lightnow-proxy`
- PyPI project name: `lightnow-proxy`
- Installed command: `lightnow-proxy`
- License: `Apache-2.0`
- Topics: `lightnow`, `mcp`, `model-context-protocol`, `local-proxy`

Keep the installed command as `lightnow-proxy` so generated MCP client configs
use a LightNow-owned executable name and do not collide with generic MCP proxy
packages.

## Checklist

Before publishing a release:

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
7. Update the Homebrew tap after the release artifact is available:
   - repository: `lightnow-ai/homebrew-tap`
   - formula: `Formula/lightnow-proxy.rb`
   - replace the formula template URL, version and SHA256 with the released artifact,
   - run `brew audit`, `brew install --build-from-source`, and `brew test`.
8. Verify fresh installs on a clean machine or isolated tool environment:
   ```bash
   uv tool install lightnow-proxy
   lightnow-proxy --version
   pipx install lightnow-proxy
   lightnow-proxy --version
   ```
9. Verify one local active upstream check:
   ```bash
   lightnow-proxy --config ~/.lightnow/lightnow-proxy/codex.yaml --health --json
   ```
10. Verify at least one real Local Proxy sync and MCP call with the current
    LightNow CLI against the local or production Registry API.

Do not publish with long-lived local PyPI tokens.
