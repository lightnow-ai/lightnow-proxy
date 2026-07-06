from __future__ import annotations

import subprocess
import sys

from lightnow_proxy import __version__


def test_version_flag_prints_package_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lightnow_proxy.main", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == f"lightnow-proxy {__version__}"
