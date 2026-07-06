from __future__ import annotations

import subprocess
import sys

from lightnow_proxy import __version__


def test_version_flag_prints_package_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lightnow_proxy.main", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == f"lightnow-proxy {__version__}"


def test_health_flag_prints_json_and_uses_degraded_exit_code_for_empty_profile(tmp_path) -> None:
    config_path = tmp_path / "proxy.yaml"
    config_path.write_text(
        """
auth:
  enabled: false
  issuer: https://auth.example.test/realms/example
local_proxy:
  enabled: true
  profile: default
profiles:
  default: {}
upstreams: {}
""".strip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lightnow_proxy.main",
            "--config",
            str(config_path),
            "--health",
            "--json",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert '"status": "degraded"' in result.stdout
    assert '"warning": "profile has no upstream MCP servers"' in result.stdout
