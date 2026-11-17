#!/usr/bin/env python3
"""Prepare a reversible in-place rename of an existing Daochi service.

Only service, executable and configuration names move. Every value (including
relative DB, key and blob paths) and the service working directory stays intact.
The original installation is retained. Never copy or rename a live database.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def systemctl(*args, check=True):
    return subprocess.run(["systemctl", *args], check=check, capture_output=True, text=True)


def copy_owned(source, target):
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target, symlinks=True)
        pairs = [(source, target)] + [(p, target / p.relative_to(source)) for p in source.rglob("*")]
    else:
        shutil.copy2(source, target, follow_symlinks=False)
        pairs = [(source, target)]
    for old, new in pairs:
        stat = old.lstat()
        os.chown(new, stat.st_uid, stat.st_gid, follow_symlinks=False)


def renamed_unit(text):
    # Rewrite command executables and EnvironmentFile directives only. A DB or
    # identity path in an argument/Environment value may share the old prefix.
    command = re.compile(
        r"^(\s*Exec(?:Start|StartPre|StartPost|Stop|StopPost|Reload)=\s*[-+!:@]*)"
        r'''(["']?)/opt/daochi/daochi\2(?=\s|$)'''
    )
    environment = re.compile(
        r'^(\s*EnvironmentFile=\s*-?)([\"\']?)/etc/daochi/daochi.env\2(\s*)$'
    )
    lines = []
    for line in text.splitlines(keepends=True):
        line = command.sub(r"\g<1>\2/opt/mesh/mesh\2", line)
        line = environment.sub(r"\g<1>\2/etc/mesh/mesh.env\2\3", line)
        lines.append(line)
    return "".join(lines)


def prepare(root, stamp):
    path = lambda name: root / name.lstrip("/")
    old_env = path("/etc/daochi/daochi.env")
    new_env = path("/etc/mesh/mesh.env")
    marker = path(f"/var/lib/mesh/backups/legacy-migration.{stamp}.json")
    if new_env.exists():
        if systemctl("is-active", "--quiet", "daochi.service", check=False).returncode == 0:
            raise ValueError("Daochi is still running beside Mesh configuration; stop it before upgrading")
        return
    if not old_env.is_file():
        raise ValueError("No installed environment: provision /etc/mesh/mesh.env before installing Mesh")
    if old_env.is_symlink() or any(path(p).is_symlink() for p in ("/etc/daochi", "/opt/daochi")):
        raise ValueError("Legacy installation uses symlink roots; provision independent Mesh configuration before upgrading")
    for candidate in path("/etc/daochi").rglob("*"):
        if candidate.is_file() and not candidate.is_symlink():
            with candidate.open("rb") as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    raise ValueError("Legacy configuration directory contains a database; provision independent Mesh configuration without copying state")
    source_unit = next((path(p) for p in ("/etc/systemd/system/daochi.service", "/usr/lib/systemd/system/daochi.service", "/lib/systemd/system/daochi.service") if path(p).is_file()), None)
    if source_unit is None:
        raise ValueError("Cannot locate the installed Daochi service unit")
    targets = [path(p) for p in ("/etc/mesh", "/opt/mesh", "/etc/systemd/system/mesh.service", "/etc/systemd/system/mesh.service.d")]
    if any(p.exists() or p.is_symlink() for p in targets):
        raise ValueError("Mesh destination already exists; refusing to overlay installation")
    active = systemctl("is-active", "--quiet", "daochi.service", check=False).returncode == 0
    enabled = systemctl("is-enabled", "--quiet", "daochi.service", check=False).returncode == 0
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker.write_text(json.dumps({"active": active, "enabled": enabled, "targets": [str(p.relative_to(root)) for p in targets]}))
    marker.chmod(0o600)
    try:
        copy_owned(path("/etc/daochi"), path("/etc/mesh"))
        copied_env = path("/etc/mesh/daochi.env")
        copied_env.rename(new_env)
        new_env.chmod(0o600)
        # A relative/default DB may live in /opt/daochi. Copy only executable
        # artifacts; keep all state at its original physical location.
        old_install = path("/opt/daochi")
        new_install = path("/opt/mesh")
        new_install.mkdir()
        shutil.copystat(old_install, new_install)
        owner = old_install.stat()
        os.chown(new_install, owner.st_uid, owner.st_gid)
        copy_owned(old_install / "daochi", new_install / "mesh")
        manifest = old_install / "daochi.manifest.json"
        if manifest.exists():
            copy_owned(manifest, new_install / "mesh.manifest.json")
        unit = path("/etc/systemd/system/mesh.service")
        copy_owned(source_unit, unit)
        old_dropins = path("/etc/systemd/system/daochi.service.d")
        new_dropins = path("/etc/systemd/system/mesh.service.d")
        if old_dropins.exists():
            copy_owned(old_dropins, new_dropins)
        for p in [unit, *new_dropins.glob("*.conf")]:
            p.write_text(renamed_unit(p.read_text()))
        # The installer snapshots SQLite only after both service names are stopped.
        systemctl("stop", "daochi.service")
        systemctl("disable", "daochi.service")
        systemctl("daemon-reload")
        if enabled:
            systemctl("enable", "mesh.service")
    except BaseException:
        restore(root, stamp)
        raise


def restore(root, stamp):
    path = lambda name: root / name.lstrip("/")
    marker = path(f"/var/lib/mesh/backups/legacy-migration.{stamp}.json")
    if not marker.exists():
        return
    state = json.loads(marker.read_text())
    systemctl("stop", "mesh.service", check=False)
    systemctl("disable", "mesh.service", check=False)
    # These were empty/absent before prepare; originals and snapshots remain.
    for name in ("/etc/mesh", "/opt/mesh", "/etc/systemd/system/mesh.service", "/etc/systemd/system/mesh.service.d"):
        target = path(name)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
    systemctl("daemon-reload")
    if state["enabled"]:
        systemctl("enable", "daochi.service")
    if state["active"]:
        systemctl("start", "daochi.service")
    marker.rename(marker.with_suffix(".restored.json"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "restore"))
    parser.add_argument("stamp")
    parser.add_argument("--root", type=Path, default=Path("/"))
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9TZ]{16}", args.stamp):
        parser.error("invalid backup timestamp")
    globals()[args.action](args.root, args.stamp)


if __name__ == "__main__":
    main()
