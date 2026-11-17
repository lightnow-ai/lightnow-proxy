"""Regression coverage for security risk breadth and independent native evidence."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

CI = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("risk_profile", CI / "risk_profile.py")
risk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(risk)


class ClientGateTests(unittest.TestCase):
    def test_security_and_unknown_paths_keep_full_compatibility(self):
        for path in (
            "src/auth.py",
            "src/runtime.py",
            "ci/check.py",
            "pyproject.toml",
            "uv.lock",
            "Jenkinsfile",
            "Dockerfile",
            "unrecognized.data",
        ):
            with self.subTest(path=path):
                self.assertEqual(risk.classify([path]), "high")
        self.assertEqual(risk.classify([]), "high")
        self.assertEqual(risk.classify(["docs/guide.md"]), "low")
        self.assertEqual(
            risk.classify(["src/formatting.py", "tests/test_output.py"]), "medium"
        )
        self.assertEqual(risk.classify(["docs/guide.md", "src/auth.py"]), "high")

    def inventory(self, planned, executed, skip=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "test-reports"
            root.mkdir()
            (root / "planned-pytest.json").write_text(json.dumps(planned))
            suite = ET.Element("testsuite")
            for nodeid in executed:
                case = ET.SubElement(suite, "testcase")
                properties = ET.SubElement(case, "properties")
                ET.SubElement(
                    properties, "property", name="lightnow_nodeid", value=nodeid
                )
                if skip:
                    ET.SubElement(case, "skipped")
            ET.ElementTree(suite).write(root / "junit.xml")
            environment = {
                key: value for key, value in os.environ.items() if key != "GITHUB_ENV"
            }
            return subprocess.run(
                [sys.executable, str(CI / "verify_test_inventory.py")],
                cwd=directory,
                env=environment,
                capture_output=True,
            ).returncode

    def test_required_collection_and_execution_must_match(self):
        self.assertEqual(self.inventory(["test::case"], ["test::case"]), 0)
        self.assertNotEqual(
            self.inventory(["test::case", "test::lost"], ["test::case"]), 0
        )
        self.assertNotEqual(
            self.inventory(["test::case"], ["test::case", "test::extra"]), 0
        )
        self.assertNotEqual(
            self.inventory(["test::case"], ["test::case", "test::case"]), 0
        )
        self.assertNotEqual(self.inventory([], []), 0)

    def test_unreviewed_skip_cannot_satisfy_required_evidence(self):
        self.assertNotEqual(
            self.inventory(["test::case"], ["test::case"], skip=True), 0
        )


if __name__ == "__main__":
    unittest.main()
