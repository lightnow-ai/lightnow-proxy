from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from filelock import FileLock, Timeout as FileLockTimeout

from lightnow_proxy.config import RuntimeFileConfig, UpstreamConfig


RUNTIME_DIR_TOKEN = "${LIGHTNOW_RUNTIME_DIR}"


class RuntimeFileError(ValueError):
    """Managed runtime files could not be materialized safely."""


def prepare_runtime_files(config: UpstreamConfig) -> UpstreamConfig:
    if not config.runtime_files:
        return config
    if not config.runtime_files_root or not config.runtime_files_namespace:
        raise RuntimeFileError("Managed runtime files are missing their runtime identity")
    if "LIGHTNOW_RUNTIME_DIR" in config.env:
        raise RuntimeFileError("LIGHTNOW_RUNTIME_DIR is reserved for managed runtime files")

    runtime_dir = materialize_runtime_files(
        Path(config.runtime_files_root),
        config.runtime_files_namespace,
        config.runtime_files,
    )
    runtime_dir_value = str(runtime_dir)

    def substitute(value: str) -> str:
        return value.replace(RUNTIME_DIR_TOKEN, runtime_dir_value)

    return config.model_copy(
        update={
            "args": [substitute(value) for value in config.args],
            "env": {
                **{key: substitute(value) for key, value in config.env.items()},
                "LIGHTNOW_RUNTIME_DIR": runtime_dir_value,
            },
            "cwd": substitute(config.cwd) if config.cwd else None,
        }
    )


def materialize_runtime_files(
    root: Path,
    namespace: str,
    files: list[RuntimeFileConfig],
) -> Path:
    root = root.expanduser().resolve()
    namespace_digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
    content_digest = _content_digest(files)
    namespace_dir = root / namespace_digest
    target_dir = namespace_dir / content_digest
    lock_dir = root / ".locks"

    _ensure_private_directory(root)
    _ensure_private_directory(lock_dir)
    lock = FileLock(str(lock_dir / f"{namespace_digest}.lock"), timeout=10)
    try:
        with lock:
            _ensure_private_directory(namespace_dir)
            if target_dir.exists():
                _verify_materialized_files(target_dir, files)
                return target_dir

            temporary_dir = Path(tempfile.mkdtemp(prefix=".tmp-", dir=namespace_dir))
            os.chmod(temporary_dir, 0o700)
            try:
                for runtime_file in files:
                    destination = temporary_dir / runtime_file.path
                    _ensure_private_directory(destination.parent)
                    with destination.open("x", encoding="utf-8", newline="") as handle:
                        handle.write(runtime_file.content)
                    os.chmod(destination, 0o600)
                os.replace(temporary_dir, target_dir)
            except BaseException:
                _remove_empty_or_partial_tree(temporary_dir)
                raise
    except FileLockTimeout as exc:
        raise RuntimeFileError("Timed out while materializing managed runtime files") from exc

    _verify_materialized_files(target_dir, files)
    return target_dir


def _content_digest(files: list[RuntimeFileConfig]) -> str:
    payload = [item.model_dump(mode="json") for item in sorted(files, key=lambda item: item.path)]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeFileError("Managed runtime file path is not a private directory")
    os.chmod(path, 0o700)


def _verify_materialized_files(target_dir: Path, files: list[RuntimeFileConfig]) -> None:
    if target_dir.is_symlink() or not target_dir.is_dir():
        raise RuntimeFileError("Managed runtime file directory failed its integrity check")

    expected = {item.path: item.content for item in files}
    observed: set[str] = set()
    for candidate in target_dir.rglob("*"):
        if candidate.is_symlink():
            raise RuntimeFileError("Managed runtime file directory contains a symbolic link")
        if candidate.is_dir():
            os.chmod(candidate, 0o700)
            continue
        relative = candidate.relative_to(target_dir).as_posix()
        observed.add(relative)
        if relative not in expected or candidate.read_text(encoding="utf-8") != expected[relative]:
            raise RuntimeFileError("Managed runtime file directory failed its integrity check")
        os.chmod(candidate, 0o600)
    if observed != set(expected):
        raise RuntimeFileError("Managed runtime file directory failed its integrity check")


def _remove_empty_or_partial_tree(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    for candidate in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
        elif candidate.is_dir():
            candidate.rmdir()
    path.rmdir()
