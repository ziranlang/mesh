#!/usr/bin/env python3
"""Generate the server's Go from its Ziran sources.

The Go is never committed: its files end in .zi.go (tests .zi_test.go), which
.gitignore leaves out, and builds regenerate it whenever a source changes.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def sources_in(repo):
    """Return the application and Ziran test entry paths relative to Mesh."""
    return sorted(str(path.relative_to(repo)) for directory in ("src", "tests/ziran")
                  for path in (repo / directory).rglob("*.zi"))


def project_flags(repo):
    if (repo / "ziran.local.toml").is_file():
        return ["--project"]
    return ["--project", "--locked"]


def generate_go(ziran, repo, sources, output, env, source_root=None):
    """Keep the public library surface and obtain the native entry from zi2go."""
    arguments = [ziran, "build", *project_flags(repo), "--target=go", "--pkg", "main"]
    if source_root is not None:
        arguments.extend(["--root", str(source_root)])
    subprocess.run([*arguments, "--no-main", "-o", str(output), *sources],
                   cwd=repo, env=env, check=True)
    with tempfile.TemporaryDirectory(prefix="entry-go-", dir=repo / "build") as entry:
        subprocess.run([*arguments, "--exe", "--entry", "main:Main", "-o", entry, *sources],
                       cwd=repo, env=env, check=True)
        shutil.copyfile(Path(entry) / "ziran_entry.go", Path(output) / "ziran_entry.go")
    # These modules accompany the opt-in C ABI host and use Unix file locks.
    for name in ("embedded_profile.go", "embedded_runtime.go"):
        path = Path(output) / name
        path.write_text("//go:build mesh_embedded\n\n" + path.read_text())


def repo_name(name):
    """The checkout's name for a generated file, which .gitignore leaves out."""
    if name.endswith("_test.go"):
        return name[:-len("_test.go")] + ".zi_test.go"
    return name[:-len(".go")] + ".zi.go"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ziran", default=os.environ.get("ZIRAN", "ziran"))
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    sources = sources_in(repo)
    if not sources:
        parser.error("no Ziran server sources found")
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env["LDLIBS"] = "-loqs"
    build = repo / "build"
    build.mkdir(exist_ok=True)
    env.setdefault("XDG_CACHE_HOME", str(build / "package-cache"))
    with tempfile.TemporaryDirectory(prefix="zi-go-", dir=build) as output:
        generate_go(args.ziran, repo, sources, output, env)
        generated = sorted(Path(output).glob("*.go"))
        subprocess.run(["gofmt", "-w", *(str(path) for path in generated)],
                       cwd=repo, env=env, check=True)
        expected = {repo_name(path.name) for path in generated}
        for path in [*repo.glob("*.zi.go"), *repo.glob("*.zi_test.go")]:
            if path.name not in expected:
                path.unlink()
        for path in generated:
            target = repo / repo_name(path.name)
            if not target.exists() or target.read_bytes() != path.read_bytes():
                shutil.copyfile(path, target)
    # Make compares this file's time with the sources'.
    (repo / "ziran_entry.zi.go").touch()
    print(f"Ziran Go generated: {len(generated)} files")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
