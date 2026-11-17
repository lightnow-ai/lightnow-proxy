"""Preserve pytest's native identity in independent JUnit coverage evidence."""

import json
from pathlib import Path


def pytest_collection_finish(session):
    target = Path("test-reports/planned-pytest.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([item.nodeid for item in session.items]))
    for item in session.items:
        item.user_properties.append(("lightnow_nodeid", item.nodeid))
