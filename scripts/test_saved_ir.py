#!/usr/bin/env python3
"""Run the real server suite against Go regenerated from saved Ziran IR."""

import os
from pathlib import Path
import subprocess
import tempfile

from generate_go import generate_go, project_flags, repo_name, sources_in
from test_go import run_tests


def main():
    repo = Path(__file__).resolve().parent.parent
    ziran = os.environ.get("ZIRAN", "ziran")
    sources = sources_in(repo)
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env["LDLIBS"] = "-loqs"
    build = repo / "build"
    build.mkdir(exist_ok=True)
    env.setdefault("XDG_CACHE_HOME", str(build / "package-cache"))
    with tempfile.TemporaryDirectory(prefix="saved-ir-", dir=build) as output:
        work = Path(output)
        ir = work / "ir"
        generated = work / "go"
        subprocess.run([ziran, "ir", *project_flags(repo),
                        "-o", str(ir), *sources], cwd=repo, env=env, check=True)
        entries = [str(ir / Path(source).with_suffix(".zir").name) for source in sources]
        generate_go(ziran, repo, entries, generated, env, source_root=ir)
        replacement = {str(repo / repo_name(path.name)): str(path)
                       for path in generated.glob("*.go")}
        run_tests(repo, ["-count=1"], env, replacement)
    print("Server tests against saved Ziran IR: passed")


if __name__ == "__main__":
    main()
