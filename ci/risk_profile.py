"""Choose compatibility breadth; every selected version runs the full suite."""

import json
import subprocess
import sys


def classify(paths):
    if not paths:
        return "high"
    if all(
        path.startswith("docs/") or path.endswith((".md", ".rst")) for path in paths
    ):
        return "low"
    critical = ("auth", "secret", "router", "config", "main.py", "runtime", "tls")
    if any(
        path.startswith((".github/", "ci/"))
        or path in ("pyproject.toml", "uv.lock", "Jenkinsfile", "Dockerfile")
        or any(word in path.lower() for word in critical)
        for path in paths
    ):
        return "high"
    return "medium" if all(path.endswith(".py") for path in paths) else "high"


if __name__ == "__main__":
    paths = (
        subprocess.check_output(
            ["git", "diff", "--name-only", "-z", sys.argv[1], sys.argv[2]]
        )
        .decode()
        .split("\0")[:-1]
    )
    risk = classify(paths) if sys.argv[3] == "pull_request" else "high"
    versions = {
        "low": ["3.12"],
        "medium": ["3.11", "3.12"],
        "high": ["3.11", "3.12", "3.13"],
    }[risk]
    print(f"risk={risk}")
    print("matrix=" + json.dumps({"python-version": versions}))
