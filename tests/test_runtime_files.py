from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from lightnow_proxy.config import UpstreamConfig
from lightnow_proxy.runtime_files import RuntimeFileError, prepare_runtime_files


def runtime_config(root: Path, content: str = "readonly = true\n") -> UpstreamConfig:
    return UpstreamConfig(
        transport="stdio",
        command="dbhub",
        args=["--config", "${LIGHTNOW_RUNTIME_DIR}/dbhub.toml"],
        env={"DB_PASSWORD": "${DB_PASSWORD}"},
        cwd="${LIGHTNOW_RUNTIME_DIR}",
        runtime_files=[{"path": "dbhub.toml", "content": content}],
        runtime_files_root=str(root),
        runtime_files_namespace="default|dbhub|analytics",
    )


def test_materializes_private_content_addressed_files_and_substitutes_launch_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_PASSWORD", "not-written-to-runtime-file")

    prepared = prepare_runtime_files(runtime_config(tmp_path))
    runtime_dir = Path(prepared.env["LIGHTNOW_RUNTIME_DIR"])

    assert prepared.args == ["--config", f"{runtime_dir}/dbhub.toml"]
    assert prepared.cwd == str(runtime_dir)
    assert prepared.resolved_env()["DB_PASSWORD"] == "not-written-to-runtime-file"
    assert (runtime_dir / "dbhub.toml").read_text(encoding="utf-8") == "readonly = true\n"
    assert os.stat(runtime_dir).st_mode & 0o777 == 0o700
    assert os.stat(runtime_dir / "dbhub.toml").st_mode & 0o777 == 0o600


def test_changed_content_gets_a_new_immutable_revision(tmp_path: Path) -> None:
    first = Path(prepare_runtime_files(runtime_config(tmp_path, "revision = 1\n")).env["LIGHTNOW_RUNTIME_DIR"])
    second = Path(prepare_runtime_files(runtime_config(tmp_path, "revision = 2\n")).env["LIGHTNOW_RUNTIME_DIR"])

    assert first != second
    assert (first / "dbhub.toml").read_text(encoding="utf-8") == "revision = 1\n"
    assert (second / "dbhub.toml").read_text(encoding="utf-8") == "revision = 2\n"


def test_concurrent_materialization_converges_on_one_revision(tmp_path: Path) -> None:
    config = runtime_config(tmp_path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        directories = list(executor.map(lambda _: prepare_runtime_files(config).env["LIGHTNOW_RUNTIME_DIR"], range(8)))

    assert len(set(directories)) == 1


def test_rejects_unsafe_runtime_file_paths(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="invalid runtime file path"):
        UpstreamConfig(
            transport="stdio",
            command="dbhub",
            runtime_files=[{"path": "../dbhub.toml", "content": ""}],
            runtime_files_root=str(tmp_path),
            runtime_files_namespace="default|dbhub",
        )

    with pytest.raises(ValidationError, match="invalid runtime file path"):
        UpstreamConfig(
            transport="stdio",
            command="dbhub",
            runtime_files=[{"path": "dbhub/..", "content": ""}],
            runtime_files_root=str(tmp_path),
            runtime_files_namespace="default|dbhub",
        )

    with pytest.raises(ValidationError, match="invalid runtime file path"):
        UpstreamConfig(
            transport="stdio",
            command="dbhub",
            runtime_files=[{"path": "a" * 513, "content": ""}],
            runtime_files_root=str(tmp_path),
            runtime_files_namespace="default|dbhub",
        )


def test_rejects_runtime_file_content_that_cannot_be_encoded_as_utf8(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="valid UTF-8 text"):
        UpstreamConfig(
            transport="stdio",
            command="dbhub",
            runtime_files=[{"path": "dbhub.toml", "content": "\ud800"}],
            runtime_files_root=str(tmp_path),
            runtime_files_namespace="default|dbhub",
        )


def test_rejects_unknown_runtime_file_properties(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        UpstreamConfig(
            transport="stdio",
            command="dbhub",
            runtime_files=[{"path": "dbhub.toml", "content": "", "mode": "0755"}],
            runtime_files_root=str(tmp_path),
            runtime_files_namespace="default|dbhub",
        )


def test_fails_closed_when_an_immutable_revision_is_modified(tmp_path: Path) -> None:
    config = runtime_config(tmp_path)
    prepared = prepare_runtime_files(config)
    runtime_file = Path(prepared.env["LIGHTNOW_RUNTIME_DIR"]) / "dbhub.toml"
    runtime_file.write_text("tampered = true\n", encoding="utf-8")

    with pytest.raises(RuntimeFileError, match="integrity check"):
        prepare_runtime_files(config)


def test_reserves_runtime_directory_environment_name(tmp_path: Path) -> None:
    config = runtime_config(tmp_path).model_copy(
        update={"env": {"LIGHTNOW_RUNTIME_DIR": "/unmanaged", "DB_PASSWORD": "${DB_PASSWORD}"}}
    )

    with pytest.raises(RuntimeFileError, match="reserved"):
        prepare_runtime_files(config)
