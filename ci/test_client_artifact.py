"""Offline provenance contract checks; no installation or external services."""

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from create_client_artifact import build_manifest


class ClientArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "dist").mkdir()
        (self.root / "pyproject.toml").write_text(
            '[project]\nname = "lightnow-cli"\nversion = "1.2.3"\n'
        )
        self.wheel = self.root / "dist/lightnow_cli-1.2.3-py3-none-any.whl"
        self.write_wheel()

    def write_wheel(
        self,
        name: str = "lightnow-cli",
        version: str = "1.2.3",
        tag: str = "py3-none-any",
    ) -> None:
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr(
                "lightnow_cli-1.2.3.dist-info/METADATA",
                f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n",
            )
            archive.writestr(
                "lightnow_cli-1.2.3.dist-info/WHEEL",
                f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n",
            )

    def manifest(self, **overrides: object) -> dict:
        arguments = dict(
            root=self.root,
            component="cli",
            repository="lightnow-ai/lightnow-cli",
            pr_number=42,
            head_revision="a" * 40,
            checkout_revision="b" * 40,
            run_id=123,
            run_attempt=2,
        )
        arguments.update(overrides)
        return build_manifest(**arguments)

    def test_exact_contract_keeps_head_and_tested_checkout_separate(self) -> None:
        self.assertEqual(
            self.manifest(),
            {
                "schemaVersion": "platform.lightnow.ai/feature-stack-client-artifact/v1",
                "component": "cli",
                "repository": "lightnow-ai/lightnow-cli",
                "packageName": "lightnow-cli",
                "packageVersion": "1.2.3",
                "filename": self.wheel.name,
                "sha256": hashlib.sha256(self.wheel.read_bytes()).hexdigest(),
                "source": {
                    "kind": "pull-request",
                    "number": 42,
                    "headRevision": "a" * 40,
                    "checkoutRevision": "b" * 40,
                    "runId": 123,
                    "runAttempt": 2,
                    "workflowPath": ".github/workflows/ci.yml",
                },
            },
        )

    def test_rejects_unbound_identity(self) -> None:
        for arguments in (
            {"repository": "other/lightnow-cli"},
            {"component": "proxy"},
            {"head_revision": "a" * 7},
            {"checkout_revision": "not-a-revision"},
            {"pr_number": 0},
            {"run_id": -1},
            {"run_attempt": 0},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.manifest(**arguments)

    def test_rejects_metadata_or_platform_mismatch(self) -> None:
        for arguments in (
            {"name": "another-package"},
            {"version": "9.0.0"},
            {"tag": "cp312-cp312-linux_x86_64"},
        ):
            with self.subTest(arguments=arguments):
                self.write_wheel(**arguments)
                with self.assertRaises(ValueError):
                    self.manifest()

    def test_rejects_ambiguous_missing_or_symlinked_wheel(self) -> None:
        extra = self.wheel.with_name("another.whl")
        extra.write_bytes(self.wheel.read_bytes())
        with self.assertRaises(ValueError):
            self.manifest()
        extra.unlink()
        self.wheel.unlink()
        with self.assertRaises(ValueError):
            self.manifest()
        self.wheel.symlink_to(self.root / "outside.whl")
        with self.assertRaises(ValueError):
            self.manifest()

    def test_rejects_duplicate_distribution_metadata(self) -> None:
        with zipfile.ZipFile(self.wheel, "a") as archive:
            archive.writestr(
                "other-1.0.dist-info/METADATA", "Name: other\nVersion: 1.0\n"
            )
        with self.assertRaises(ValueError):
            self.manifest()


if __name__ == "__main__":
    unittest.main()
