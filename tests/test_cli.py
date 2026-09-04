from __future__ import annotations

import subprocess
import sys
import os

from lightnow_proxy import __version__
from lightnow_proxy import main as proxy_main


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


def test_health_reports_non_secret_connection_binding(tmp_path) -> None:
    config_path = tmp_path / "proxy.yaml"
    config_path.write_text(
        """
auth:
  enabled: false
  issuer: https://auth.example.test/realms/example
local_proxy:
  enabled: true
  connection_id: 10d45f35-3143-482b-bda2-7f3931667049
  connection_alias: lightnow-acme
  account_label: Developer
  scope_type: tenant
  scope_id: tenant-1
  profile: engineering
registry_api:
  enabled: false
  base_url: https://registry.example.test
  cli_session_path: /tmp/session.json
  expected_issuer: https://auth.example.test/realms/example
  expected_subject: user-1
profiles:
  engineering: {}
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
    assert '"connection_alias": "lightnow-acme"' in result.stdout
    assert '"account_label": "Developer"' in result.stdout
    assert '"scope_type": "tenant"' in result.stdout
    assert '"expected_subject": "user-1"' in result.stdout
    assert "session.json" not in result.stdout


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


def test_local_proxy_starts_stdio_without_transport_selection(tmp_path, monkeypatch) -> None:
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
    calls: list[tuple[object, tuple[object, ...]]] = []
    monkeypatch.setattr(sys, "argv", ["lightnow-proxy", "--config", str(config_path)])
    monkeypatch.setattr(proxy_main.anyio, "run", lambda func, *args: calls.append((func, args)))
    monkeypatch.setattr(proxy_main.uvicorn, "run", lambda *_args, **_kwargs: calls.append(("http", ())))

    proxy_main.main()

    assert len(calls) == 1
    assert calls[0][0] is proxy_main.run_stdio
    assert calls[0][1][2] == str(config_path)


def test_profile_proxy_still_starts_http_without_transport_selection(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "proxy.yaml"
    config_path.write_text(
        """
auth:
  enabled: false
  issuer: https://auth.example.test/realms/example
profiles:
  default: {}
upstreams: {}
""".strip(),
        encoding="utf-8",
    )
    app = object()
    calls: list[tuple[object, str, int]] = []
    monkeypatch.setattr(sys, "argv", ["lightnow-proxy", "--config", str(config_path)])
    monkeypatch.setattr(proxy_main, "create_app", lambda _config: app)
    monkeypatch.setattr(proxy_main.uvicorn, "run", lambda value, host, port: calls.append((value, host, port)))

    proxy_main.main()

    assert calls == [(app, "127.0.0.1", 8080)]
