from __future__ import annotations

import pytest

from lightnow_proxy.config import UpstreamConfig
from lightnow_proxy.diagnostics import UpstreamConfigurationError, validate_upstream_config


@pytest.mark.parametrize("tty_argument", ["-t", "-it", "-ti", "--tty", "--tty=true"])
def test_rejects_docker_tty_allocation_for_stdio(tty_argument: str) -> None:
    config = UpstreamConfig(
        transport="stdio",
        command="docker",
        args=["run", tty_argument, "--rm", "ghcr.io/github/github-mcp-server"],
    )

    with pytest.raises(UpstreamConfigurationError) as raised:
        validate_upstream_config(config)

    assert raised.value.diagnostic.code == "DOCKER_TTY_UNSUPPORTED"
    assert raised.value.diagnostic.kind == "configuration"
    assert "Keep -i" in raised.value.diagnostic.remediation


def test_rejects_missing_docker_passthrough_environment_variable(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    config = UpstreamConfig(
        transport="stdio",
        command="docker",
        args=[
            "run",
            "--rm",
            "-i",
            "-e",
            "GITHUB_PERSONAL_ACCESS_TOKEN",
            "ghcr.io/github/github-mcp-server",
        ],
    )

    with pytest.raises(UpstreamConfigurationError) as raised:
        validate_upstream_config(config)

    assert raised.value.diagnostic.code == "DOCKER_ENV_MISSING"
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in raised.value.diagnostic.remediation


def test_accepts_managed_docker_environment_variable(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    config = UpstreamConfig(
        transport="stdio",
        command="docker",
        args=[
            "run",
            "--rm",
            "-i",
            "--env=GITHUB_PERSONAL_ACCESS_TOKEN",
            "ghcr.io/github/github-mcp-server",
        ],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "managed-value"},
    )

    validate_upstream_config(config)


def test_ignores_container_arguments_after_the_image() -> None:
    config = UpstreamConfig(
        transport="stdio",
        command="docker",
        args=["run", "--rm", "-i", "example/mcp-server", "-t", "tool-name"],
    )

    validate_upstream_config(config)


def test_accepts_explicitly_disabled_docker_tty() -> None:
    config = UpstreamConfig(
        transport="stdio",
        command="docker",
        args=["run", "--rm", "-i", "--tty=false", "example/mcp-server"],
    )

    validate_upstream_config(config)
