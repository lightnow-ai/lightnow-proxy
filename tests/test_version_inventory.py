import json

from lightnow_proxy.config import LocalProxyConfig
from lightnow_proxy.version_inventory import detect_install_method, heartbeat_version_fields


def test_detect_install_method_recognizes_supported_layouts() -> None:
    assert detect_install_method("/opt/homebrew/Cellar/lightnow-proxy/1.4.2/bin/lightnow-proxy") == "homebrew"
    assert detect_install_method("/home/me/.local/pipx/venvs/lightnow-proxy/bin/lightnow-proxy") == "pipx"
    assert detect_install_method("/home/me/.local/share/uv/tools/lightnow-proxy/bin/lightnow-proxy") == "uv"
    assert detect_install_method("/tmp/venv/bin/lightnow-proxy") == "unknown"


def test_heartbeat_version_fields_prefers_valid_cached_state(tmp_path) -> None:
    state_path = tmp_path / "update-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checked_at": "2026-07-17T08:00:00Z",
                "packages": {
                    "lightnow-cli": {
                        "installed_version": "1.3.1",
                        "install_method": "homebrew",
                        "latest_version": "1.4.0",
                        "status": "outdated",
                    },
                    "lightnow-proxy": {
                        "installed_version": "1.4.2",
                        "install_method": "pipx",
                        "latest_version": "1.4.2",
                        "status": "current",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    config = LocalProxyConfig(
        update_state_path=str(state_path),
        cli_version="1.0.0",
        cli_install_method="unknown",
    )

    device, runner = heartbeat_version_fields(config)

    assert device == {
        "cli_version": "1.3.1",
        "cli_install_method": "homebrew",
        "cli_latest_version": "1.4.0",
        "cli_update_status": "outdated",
        "cli_update_checked_at": "2026-07-17T08:00:00Z",
    }
    assert runner == {
        "runner_install_method": "pipx",
        "runner_latest_version": "1.4.2",
        "runner_update_status": "current",
        "runner_update_checked_at": "2026-07-17T08:00:00Z",
    }


def test_heartbeat_version_fields_degrades_safely_for_missing_state(tmp_path) -> None:
    config = LocalProxyConfig(
        update_state_path=str(tmp_path / "missing.json"),
        cli_version="1.3.1",
        cli_install_method="uv",
    )

    device, runner = heartbeat_version_fields(config)

    assert device["cli_version"] == "1.3.1"
    assert device["cli_install_method"] == "uv"
    assert device["cli_update_status"] == "unknown"
    assert device["cli_update_checked_at"] is None
    assert runner["runner_update_status"] == "unknown"
