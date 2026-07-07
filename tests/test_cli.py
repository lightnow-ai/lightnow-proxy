from __future__ import annotations

import subprocess
import sys
import os

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


def test_health_flag_uses_default_user_config_path(tmp_path) -> None:
    home = tmp_path / "home"
    config_path = home / ".lightnow" / "lightnow-proxy" / "default.yaml"
    config_path.parent.mkdir(parents=True)
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

    env = {**os.environ, "HOME": str(home)}
    env.pop("LIGHTNOW_PROXY_CONFIG", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lightnow_proxy.main",
            "--health",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 2
    assert '"status": "degraded"' in result.stdout
    assert str(config_path) not in result.stderr


def test_missing_config_file_prints_error_instead_of_traceback(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lightnow_proxy.main",
            "--config",
            str(tmp_path / "does-not-exist.yaml"),
            "--health",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "config file not found" in result.stderr
    assert "Traceback" not in result.stderr
