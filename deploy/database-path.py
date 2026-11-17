#!/usr/bin/env python3
"""Resolve the configured store without evaluating shell expressions."""
import os
from pathlib import Path
import shlex
import subprocess
import sys


def database_path(environment, working_directory):
    settings = {}
    for line in environment.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key in ("MESH_DB", "DAOCHI_DB", "KSYNC_DB"):
            tokens = shlex.split(value, comments=False)
            if len(tokens) != 1 or not tokens[0]:
                raise ValueError("database setting must be a nonempty single path")
            settings[key] = tokens[0]
    value = next((settings[key] for key in ("MESH_DB", "DAOCHI_DB", "KSYNC_DB") if key in settings), "daochi.db")
    path = Path(value)
    if not path.is_absolute():
        path = Path(working_directory) / path
    return str(path)


if __name__ == "__main__":
    directory = subprocess.check_output(["systemctl", "show", "mesh.service", "-p", "WorkingDirectory", "--value"], text=True).strip()
    if not directory or not os.path.isabs(directory):
        raise SystemExit("mesh.service needs an absolute WorkingDirectory for safe database backup")
    declared = subprocess.check_output(["systemctl", "show", "mesh.service", "-p", "Environment", "--value"], text=True)
    values = []
    for entry in shlex.split(declared):
        key, separator, value = entry.partition("=")
        if separator:
            values.append(key + "=" + shlex.quote(value))
    print(database_path("\n".join(values) + "\n" + Path(sys.argv[1]).read_text(), directory))
