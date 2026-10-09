"""Bind a PR build to its tested checkout and one universal Python wheel."""

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

SCHEMA_VERSION = "platform.lightnow.ai/feature-stack-client-artifact/v1"
MANIFEST_FILENAME = "feature-stack-client-artifact.json"
COMPONENTS = {"cli": "lightnow-cli", "proxy": "lightnow-proxy"}


def build_manifest(
    root: Path,
    component: str,
    repository: str,
    pr_number: int,
    head_revision: str,
    checkout_revision: str,
    run_id: int,
    run_attempt: int,
) -> dict:
    """Validate wheel contents and runner identity before creating provenance."""
    package_name = COMPONENTS.get(component)
    if package_name is None or repository != f"lightnow-ai/{package_name}":
        raise ValueError("Component and repository do not match")
    if any(value <= 0 for value in (pr_number, run_id, run_attempt)):
        raise ValueError("PR number, run ID and run attempt must be positive")
    if any(
        re.fullmatch(r"[0-9a-f]{40}", value) is None
        for value in (head_revision, checkout_revision)
    ):
        raise ValueError("Head and checkout revisions must be full Git SHA-1 values")

    with (root / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)["project"]
    if project["name"] != package_name:
        raise ValueError("Repository package metadata does not match component")

    wheels = sorted((root / "dist").glob("*.whl"))
    if len(wheels) != 1 or wheels[0].is_symlink() or not wheels[0].is_file():
        raise ValueError("Exactly one regular wheel is required")
    wheel = wheels[0]
    wheel_package = package_name.replace("-", "_")
    expected_filename = f"{wheel_package}-{project['version']}-py3-none-any.whl"
    if wheel.name != expected_filename:
        raise ValueError("Expected the repository's universal py3-none-any wheel")

    prefix = f"{wheel_package}-{project['version']}.dist-info/"
    with zipfile.ZipFile(wheel) as archive:
        for name in (f"{prefix}METADATA", f"{prefix}WHEEL"):
            if archive.namelist().count(name) != 1:
                raise ValueError("Wheel metadata must be unique and match its filename")
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if metadata_files != [f"{prefix}METADATA"]:
            raise ValueError("Wheel must contain exactly one distribution's metadata")
        metadata = Parser().parsestr(archive.read(f"{prefix}METADATA").decode("utf-8"))
        wheel_metadata = Parser().parsestr(
            archive.read(f"{prefix}WHEEL").decode("utf-8")
        )
    if metadata.get_all("Name") != [package_name] or metadata.get_all("Version") != [
        project["version"]
    ]:
        raise ValueError("Wheel METADATA name/version do not match repository metadata")
    if wheel_metadata.get_all("Tag") != ["py3-none-any"] or wheel_metadata.get_all(
        "Root-Is-Purelib"
    ) != ["true"]:
        raise ValueError("Wheel must declare the universal pure-Python tag")

    return {
        "schemaVersion": SCHEMA_VERSION,
        "component": component,
        "repository": repository,
        "packageName": metadata["Name"],
        "packageVersion": metadata["Version"],
        "filename": wheel.name,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "source": {
            "kind": "pull-request",
            "number": pr_number,
            "headRevision": head_revision,
            "checkoutRevision": checkout_revision,
            "runId": run_id,
            "runAttempt": run_attempt,
            "workflowPath": ".github/workflows/ci.yml",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=tuple(COMPONENTS), required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--head-revision", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    checkout_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    manifest = build_manifest(
        root,
        args.component,
        args.repository,
        args.pr_number,
        args.head_revision,
        checkout_revision,
        args.run_id,
        args.run_attempt,
    )
    (root / "dist" / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
