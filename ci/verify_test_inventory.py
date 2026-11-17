"""Fail closed when independent pytest collection and JUnit execution differ."""

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path("test-reports")
planned = json.loads((root / "planned-pytest.json").read_text())
cases = list(ET.parse(root / "junit.xml").iter("testcase"))
actual = []
for case in cases:
    identities = [
        p.get("value")
        for p in case.findall("./properties/property")
        if p.get("name") == "lightnow_nodeid"
    ]
    if len(identities) != 1 or case.find("skipped") is not None:
        raise SystemExit("Missing native testcase identity or unreviewed skipped case")
    actual.extend(identities)
if (
    not planned
    or len(set(planned)) != len(planned)
    or len(set(actual)) != len(actual)
    or set(planned) != set(actual)
):
    raise SystemExit("Collected and executed pytest inventories differ")
# Both clients currently have no skipped tests; skips require a reviewed policy change.
count = len(planned)
if os.environ.get("GITHUB_ENV"):
    with open(os.environ["GITHUB_ENV"], "a") as stream:
        stream.write(f"ALLURE_EXPECTED_PASSED={count}\n")
print(f"Verified {count} required native pytest identities; no skipped baseline")
