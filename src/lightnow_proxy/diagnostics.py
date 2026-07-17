from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal

import httpx

from lightnow_proxy.config import UpstreamConfig


DiagnosticKind = Literal[
    "configuration",
    "authentication",
    "authorization",
    "connectivity",
    "timeout",
    "runtime",
]


@dataclass(frozen=True)
class RuntimeDiagnostic:
    code: str
    kind: DiagnosticKind
    summary: str
    remediation: str


class UpstreamConfigurationError(ValueError):
    def __init__(self, diagnostic: RuntimeDiagnostic):
        super().__init__(diagnostic.summary)
        self.diagnostic = diagnostic


def validate_upstream_config(config: UpstreamConfig) -> None:
    if config.transport != "stdio":
        return

    try:
        resolved_env = config.resolved_env()
    except ValueError as exc:
        names = ", ".join(sorted(config.env)) or "the configured variable"
        raise UpstreamConfigurationError(
            RuntimeDiagnostic(
                code="ENV_REFERENCE_UNRESOLVED",
                kind="configuration",
                summary="A required environment reference could not be resolved.",
                remediation=f"Configure {names} in the Runtime Profile or the local proxy environment.",
            )
        ) from exc

    if Path(config.command or "").name != "docker" or not config.args or config.args[0] != "run":
        return

    docker_options = _docker_run_options(config.args[1:])
    if docker_options.allocates_tty:
        raise UpstreamConfigurationError(
            RuntimeDiagnostic(
                code="DOCKER_TTY_UNSUPPORTED",
                kind="configuration",
                summary="Docker TTY allocation is incompatible with MCP stdio.",
                remediation="Remove -t or --tty from the Docker arguments. Keep -i so the MCP server can use stdin.",
            )
        )

    available_env = set(os.environ) | set(resolved_env)
    missing_env = sorted(name for name in docker_options.passthrough_env if name not in available_env)
    if missing_env:
        names = ", ".join(missing_env)
        raise UpstreamConfigurationError(
            RuntimeDiagnostic(
                code="DOCKER_ENV_MISSING",
                kind="configuration",
                summary="A Docker environment variable required by this MCP server is not available.",
                remediation=f"Configure {names} as a managed or external secret in the Runtime Profile.",
            )
        )


def diagnostic_for_exception(exc: BaseException) -> RuntimeDiagnostic:
    if isinstance(exc, UpstreamConfigurationError):
        return exc.diagnostic

    if isinstance(exc, FileNotFoundError):
        return RuntimeDiagnostic(
            code="COMMAND_NOT_FOUND",
            kind="configuration",
            summary="The configured MCP server command was not found on this device.",
            remediation="Install the command on this device or update the Runtime Profile to use an available executable.",
        )

    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return RuntimeDiagnostic(
            code="UPSTREAM_TIMEOUT",
            kind="timeout",
            summary="The MCP server did not respond before the timeout.",
            remediation="Check that the server is reachable and increase the configured timeout only if startup is expected to be slow.",
        )

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return RuntimeDiagnostic(
                code="UPSTREAM_AUTHENTICATION_FAILED",
                kind="authentication",
                summary="The MCP server rejected the configured credentials.",
                remediation="Update the credential or secret used by this Runtime Profile, then run the health check again.",
            )
        if status == 403:
            return RuntimeDiagnostic(
                code="UPSTREAM_AUTHORIZATION_FAILED",
                kind="authorization",
                summary="The configured identity is not allowed to access this MCP server.",
                remediation="Grant the required permissions or use credentials with access to this server.",
            )

    if "connection closed" in (str(exc) or "").lower():
        return RuntimeDiagnostic(
            code="UPSTREAM_CONNECTION_CLOSED",
            kind="connectivity",
            summary="The MCP server closed the connection during startup.",
            remediation="Check the server command, required environment variables, and local runtime logs, then run the health check again.",
        )

    return RuntimeDiagnostic(
        code="UPSTREAM_START_FAILED",
        kind="runtime",
        summary="The MCP server could not be started successfully.",
        remediation="Review the technical error and the server's local logs, correct the configuration, and run the health check again.",
    )


@dataclass(frozen=True)
class _DockerRunOptions:
    allocates_tty: bool
    passthrough_env: frozenset[str]


_OPTIONS_WITH_VALUE = {
    "--add-host",
    "--cap-add",
    "--device",
    "--entrypoint",
    "--env-file",
    "--hostname",
    "--label",
    "--mount",
    "--name",
    "--network",
    "--platform",
    "--publish",
    "--pull",
    "--security-opt",
    "--user",
    "--volume",
    "--workdir",
    "-h",
    "-l",
    "-p",
    "-u",
    "-v",
    "-w",
}


def _docker_run_options(args: list[str]) -> _DockerRunOptions:
    allocates_tty = False
    passthrough_env: set[str] = set()
    index = 0

    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if not arg.startswith("-"):
            break

        if arg in {"--tty", "--tty=true"} or (
            arg.startswith("-") and not arg.startswith("--") and "t" in arg[1:].split("=", 1)[0]
        ):
            allocates_tty = True

        if arg in {"-e", "--env"}:
            if index + 1 < len(args):
                _add_passthrough_env(passthrough_env, args[index + 1])
                index += 2
                continue
        elif arg.startswith("--env="):
            _add_passthrough_env(passthrough_env, arg.removeprefix("--env="))
        elif arg.startswith("-e") and len(arg) > 2:
            _add_passthrough_env(passthrough_env, arg[2:])
        elif arg in _OPTIONS_WITH_VALUE and index + 1 < len(args):
            index += 2
            continue

        index += 1

    return _DockerRunOptions(allocates_tty=allocates_tty, passthrough_env=frozenset(passthrough_env))


def _add_passthrough_env(names: set[str], specification: str) -> None:
    if "=" in specification:
        return
    name = specification.strip()
    if name:
        names.add(name)
