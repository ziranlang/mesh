import fcntl
import hashlib
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


def main(binary):
    binary = Path(binary).resolve()
    # A library host has its own settings. Poison process server settings and
    # HTTP proxy configuration so the fixture also checks profile isolation.
    env = os.environ | {"MESH_ADDR": "invalid", "MESH_DB": "/dev/null", "MESH_TOKEN_SECRET_HEX": "invalid",
                        "MESH_LAN_DISCOVERY": "1", "http_proxy": "http://127.0.0.1:1", "no_proxy": ""}

    def run(*arguments):
        result = subprocess.run([binary, *arguments], env=env, text=True, capture_output=True, timeout=45)
        assert result.returncode == 0, (arguments, result.returncode, result.stderr)
        return result

    with TemporaryDirectory(dir=binary.parent) as temporary:
        root = Path(temporary)
        first = root / "nested" / "first"
        second = root / "second"
        node_id = run("lifecycle", first, f"{first}/./", second).stdout.strip()
        key = bytes.fromhex((first / "node-key").read_text())
        assert len(key) == 64 and hashlib.sha256(key[32:]).hexdigest() == node_id
        for name in ("node-key", "node.lock", "token-secret", "admin-secret", "mesh.sqlite"):
            assert (first / name).stat().st_mode & 0o777 == 0o600
        assert first.stat().st_mode & 0o777 == 0o700
        with closing(sqlite3.connect(first / "mesh.sqlite")) as database:
            assert database.execute("SELECT COUNT(*) FROM issued_pairing_invites").fetchone() == (1,)
            assert database.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        with closing(sqlite3.connect(second / "mesh.sqlite")) as database:
            assert database.execute("SELECT COUNT(*) FROM issued_pairing_invites").fetchone() == (0,)
        with (first / "node.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run("reject", first)
        public = root / "public"
        public.mkdir()
        public.chmod(0o755)
        run("reject", public)
        linked = root / "linked"
        linked.symlink_to(first, target_is_directory=True)
        run("reject", linked / "no-write")
        assert not (first / "no-write").exists()
        unsafe = root / "unsafe"
        unsafe.mkdir(mode=0o700)
        (unsafe / "node-key").symlink_to(first / "node-key")
        run("reject", unsafe)
        assert (first / "node-key").read_bytes().strip() == key.hex().encode()
        assert not (unsafe / "mesh.sqlite").exists()
    print("Native embedded host ABI, private profiles, loopback lifecycle and durable identity passed")


if __name__ == "__main__":
    main(sys.argv[1])
