#!/usr/bin/env python3
"""Run handwritten Go and generated Ziran tests in the server package."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def run_tests(repo, arguments, env, generated=None):
    # Keep the original package and working directory for native fixtures.
    # Go skips the _go directory in ordinary package discovery.
    replacement = dict(generated or {})
    for test in sorted((repo / "tests/_go").glob("*_test.go")):
        replacement[str(repo / test.name)] = str(test)
    packages = ["."]
    for directory in (repo / "native", *(repo / "testdata").iterdir()):
        if directory.is_dir() and any(directory.glob("*.go")):
            packages.append("./" + directory.relative_to(repo).as_posix())
    build = repo / "build"
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="go-tests-", dir=build) as output:
        overlay = Path(output) / "overlay.json"
        overlay.write_text(json.dumps({"Replace": replacement}))
        subprocess.run(
            [env.get("GO", "go"), "test", "-overlay", str(overlay), *arguments, *sorted(packages)],
            cwd=repo, env={**env, "MESH_GO_TEST_OVERLAY": str(overlay)}, check=True,
        )


if __name__ == "__main__":
    run_tests(Path(__file__).resolve().parents[1], sys.argv[1:], os.environ)
