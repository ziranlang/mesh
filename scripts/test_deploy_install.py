"""Exercise installation and rollback using disposable files and fake services."""

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
STAMP = "20261031T000000Z"


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = dict(os.environ, TEST_ROOT=str(self.root))
        self.environment["PATH"] = str(self.root / "bin") + os.pathsep + os.environ["PATH"]
        for name in ("opt/mesh", "opt/mesh-src/deploy", "opt/mesh-src/scripts", "opt/mesh-src/build",
                     "var/lib/mesh", "etc/mesh", "etc/systemd/system/mesh.service.d", "bin"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for script in ("install.sh", "rollback.sh", "harden-service.sh"):
            text = (REPOSITORY / "deploy" / script).read_text()
            # /var/lib/mesh is a prefix of /var/lib/mesh/backups only, while
            # /etc/mesh must not rewrite /etc/mesh.* siblings.
            for prefix in ("/opt/mesh", "/var/lib/mesh", "/var/lib/daochi", "/etc/mesh", "/etc/systemd"):
                text = text.replace(prefix, str(self.root) + prefix)
            (self.root / "opt/mesh-src/deploy" / script).write_text(text)
        for name, value in {
            "opt/mesh/mesh": "old binary", "opt/mesh/mesh.manifest.json": "old manifest",
            "opt/mesh-src/mesh": "new binary", "opt/mesh-src/build/mesh.manifest.json": "new manifest",
            "var/lib/mesh/mesh.db": "paid data", "etc/mesh/mesh.env": "old environment",
            "etc/systemd/system/mesh.service.d/20-mesh-mesh.conf": "old mesh setting",
            "etc/systemd/system/mesh.service.d/20-daochi-mesh.conf": "old daochi setting",
        }.items():
            (self.root / name).write_text(value)
        self.executable("opt/mesh-src/deploy/migrate-install.py", "")
        self.executable("opt/mesh-src/deploy/database-path.py", 'import os; print(os.environ["TEST_ROOT"] + "/var/lib/mesh/mesh.db")')
        for name in ("mesh-backup-prune.service", "mesh-backup-prune.timer", "node.env", "dew-ratio.env"):
            (self.root / "opt/mesh-src/deploy" / name).write_text("fixture")
        self.executable("bin/systemctl", '''
import os, pathlib, sys
command = " ".join(sys.argv[1:])
with (pathlib.Path(os.environ["TEST_ROOT"]) / "services.log").open("a") as output:
    output.write(command + "\\n")
sys.exit(1 if command == os.environ.get("FAIL_SERVICE") else 0)
''')
        self.executable("bin/sqlite3", '''
import os, pathlib, shlex, shutil, sys
assert "stop mesh.service" in (pathlib.Path(os.environ["TEST_ROOT"]) / "services.log").read_text()
shutil.copyfile(sys.argv[1], shlex.split(sys.argv[2])[1])
''')
        self.executable("bin/sleep", "")
        for name in ("chown", "chgrp"):
            self.executable(f"bin/{name}", '''
import os, pathlib, sys
with (pathlib.Path(os.environ["TEST_ROOT"]) / "accounts.log").open("a") as output:
    output.write(" ".join(sys.argv[1:]) + "\\n")
''')
        self.executable("bin/groupadd", '''
import os, pathlib, sys
with (pathlib.Path(os.environ["TEST_ROOT"]) / "accounts.log").open("a") as output:
    output.write(" ".join(sys.argv[1:]) + "\\n")
''')
        self.executable("bin/useradd", '''
import os, pathlib, sys
with (pathlib.Path(os.environ["TEST_ROOT"]) / "accounts.log").open("a") as output:
    output.write(" ".join(sys.argv[1:]) + "\\n")
''')
        # A disposable tree has no mesh account, so drop the owner flags and
        # keep the real install(1) for directory creation and modes.
        self.executable("bin/install", '''
import os, subprocess, sys
arguments = []
skip = False
for argument in sys.argv[1:]:
    if skip:
        skip = False
        continue
    if argument in ("-o", "-g"):
        skip = True
        continue
    arguments.append(argument)
sys.exit(subprocess.run(["/usr/bin/install", *arguments]).returncode)
''')
        self.executable("bin/getent", '''
import os, sys
# Neither the Mesh group nor the Mesh account exists yet in a disposable tree.
sys.exit(2 if sys.argv[1] in ("group", "passwd") else 1)
''')
        self.executable("opt/mesh-src/scripts/verify_deployment_inputs.py", '''
import os, sys
sys.exit(1 if os.environ.get("FAIL_VERIFY") else 0)
''')
        self.executable("opt/mesh-src/scripts/migrate-legacy-env.py", '''
import pathlib, sys
pathlib.Path(sys.argv[1]).write_text("new environment")
''')
        (self.root / "opt/mesh-src/deploy/set-environment.sh").write_text("exit 0\n")
        (self.root / "opt/mesh-src/deploy/apply-dew-ratio.sh").write_text('[ -z "${FAIL_MIGRATION:-}" ]\n')

    def executable(self, name, body):
        path = self.root / name
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o755)

    def run_install(self, token=""):
        return subprocess.run(
            ["sh", str(self.root / "opt/mesh-src/deploy/install.sh"), STAMP, "a" * 40, "b" * 40],
            input=token.encode(), env=self.environment, capture_output=True, timeout=10,
        )

    def assert_old_install(self):
        self.assertEqual((self.root / "opt/mesh/mesh").read_text(), "old binary")
        self.assertEqual((self.root / "var/lib/mesh/mesh.db").read_text(), "paid data")
        self.assertEqual((self.root / "etc/mesh/mesh.env").read_text(), "old environment")

    def test_success_backs_up_data_and_removes_token_drop_ins(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual((self.root / "opt/mesh/mesh").read_text(), "new binary")
        self.assertEqual((self.root / "opt/mesh/mesh").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / f"var/lib/mesh/backups/mesh.db.{STAMP}").read_text(), "paid data")
        # systemd shows Environment= values to local users; mesh.env keeps the token.
        for name, value in (("mesh", "old mesh setting"), ("daochi", "old daochi setting")):
            self.assertFalse((self.root / f"etc/systemd/system/mesh.service.d/20-{name}-mesh.conf").exists())
            self.assertEqual((self.root / f"var/lib/mesh/backups/mesh.{name}.{STAMP}").read_text(), value)

    def sandbox(self):
        return self.root / "etc/systemd/system/mesh.service.d/10-sandbox.conf"

    def test_install_runs_as_a_dedicated_user_in_a_sandbox(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        unit = self.sandbox().read_text()
        for setting in ("User=mesh", "Group=mesh", "NoNewPrivileges=true", "PrivateTmp=true",
                        "ProtectSystem=strict"):
            self.assertIn(setting, unit)
        self.assertIn(f"ReadWritePaths={self.root}/var/lib/daochi {self.root}/var/lib/mesh/backups", unit)
        self.assertEqual(self.sandbox().stat().st_mode & 0o777, 0o644)
        accounts = (self.root / "accounts.log").read_text()
        self.assertIn("--system --gid 987 mesh", accounts)
        self.assertIn("--system --uid 987 --gid mesh", accounts)
        # Only the data directory is owned by the service; configuration stays root's.
        self.assertIn(f"-R mesh:mesh {self.root}/var/lib/daochi", accounts)
        self.assertIn(f"-R mesh {self.root}/etc/mesh", accounts)
        self.assertNotIn(f"mesh:mesh {self.root}/etc/mesh", accounts)

    def test_failed_install_restores_the_previous_ownership_and_unit(self):
        self.sandbox().write_text("old sandbox")
        self.environment["FAIL_MIGRATION"] = "1"
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertEqual(self.sandbox().read_text(), "old sandbox")
        restoration = (self.root / "accounts.log").read_text()
        # The saved owner is restored after the service's ownership change.
        restored = [line for line in restoration.splitlines() if line.startswith(f"{os.getuid()}:{os.getgid()} ")]
        self.assertTrue(restored, restoration)
        self.assertIn(f"{os.getuid()}:{os.getgid()} {self.sandbox().parent}", restored)

    def test_deploy_token_is_refused_before_any_change(self):
        token = 'node:quote\"$()\\percent%'
        result = self.run_install(token)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"/etc/mesh/mesh.env", result.stderr)
        self.assertNotIn(token.encode(), result.stdout + result.stderr)
        self.assert_old_install()
        self.assertEqual((self.root / "etc/systemd/system/mesh.service.d/20-mesh-mesh.conf").read_text(), "old mesh setting")
        self.assertFalse((self.root / "services.log").exists())

    def test_failed_migration_restores_binary_database_environment_and_mesh(self):
        self.environment["FAIL_MIGRATION"] = "1"
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assert_old_install()
        self.assertEqual((self.root / "opt/mesh/mesh.manifest.json").read_text(), "old manifest")
        self.assertEqual((self.root / "etc/systemd/system/mesh.service.d/20-mesh-mesh.conf").read_text(), "old mesh setting")
        self.assertEqual((self.root / "etc/systemd/system/mesh.service.d/20-daochi-mesh.conf").read_text(), "old daochi setting")
        services = (self.root / "services.log").read_text()
        self.assertIn("disable --now mesh-backup-prune.timer", services)
        self.assertIn("enable daochi-backup-prune.timer", services)
        self.assertIn("start daochi-backup-prune.timer", services)

    def test_dew_migration_preserves_service_working_directory_for_relative_keys(self):
        self.executable("bin/systemctl", '''
import sys
print("node" if "User" in sys.argv else "/original service/identity")
''')
        self.executable("bin/systemd-run", '''
import json, os, pathlib, sys
(pathlib.Path(os.environ["TEST_ROOT"]) / "transient.json").write_text(json.dumps(sys.argv[1:]))
print("MONERO_RATE_TOKEN_UNITS=100")
''')
        ratio = self.root / "ratio.env"
        ratio.write_text("DEW_RESCALE=fixture\nMONERO_RATE_ATOMIC_AMOUNT=10\nMONERO_RATE_TOKEN_UNITS=100\n")
        subprocess.run(
            ["sh", str(REPOSITORY / "deploy/apply-dew-ratio.sh"), str(ratio),
             str(self.root / "etc/mesh/mesh.env"), "/opt/mesh/mesh", "/existing store/accounts.db"],
            env=self.environment, check=True, capture_output=True,
        )
        properties = json.loads((self.root / "transient.json").read_text())
        self.assertIn("WorkingDirectory=/original service/identity", properties)
        self.assertIn("Environment=MESH_DB=/existing store/accounts.db", properties)

    def test_failed_health_restores_absent_old_manifest_and_mesh(self):
        (self.root / "opt/mesh/mesh.manifest.json").unlink()
        mesh = self.root / "etc/systemd/system/mesh.service.d/20-mesh-mesh.conf"
        mesh.unlink()
        self.environment["FAIL_SERVICE"] = "is-active --quiet mesh.service"
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assert_old_install()
        self.assertFalse((self.root / "opt/mesh/mesh.manifest.json").exists())
        self.assertFalse(mesh.exists())

    def test_bad_provenance_does_not_swap_or_restart(self):
        self.environment["FAIL_VERIFY"] = "1"
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assert_old_install()
        self.assertFalse((self.root / "services.log").exists())

    def native_release(self, runtime):
        (self.root / "opt/mesh-src/mesh-native").write_text("new native")
        (self.root / "opt/mesh-src/build/mesh-native.manifest.json").write_text("native manifest")
        (self.root / "opt/mesh-src/deploy/runtime").write_text(runtime + "\n")

    def runtime_selection(self):
        return self.root / "etc/systemd/system/mesh.service.d/30-runtime.conf"

    def test_native_runtime_installs_beside_go_and_selects_it(self):
        self.native_release("native")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual((self.root / "opt/mesh/mesh").read_text(), "new binary")
        self.assertEqual((self.root / "opt/mesh/mesh-native").read_text(), "new native")
        self.assertEqual((self.root / "opt/mesh/mesh-native.manifest.json").read_text(), "native manifest")
        self.assertEqual(self.runtime_selection().read_text(),
                         f"[Service]\nExecStart=\nExecStart={self.root}/opt/mesh/mesh-native\n")
        self.assertEqual(self.runtime_selection().stat().st_mode & 0o777, 0o644)

    def test_go_runtime_removes_the_native_selection(self):
        self.runtime_selection().write_text("old runtime")
        self.native_release("go")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self.runtime_selection().exists())
        self.assertEqual((self.root / "opt/mesh/mesh-native").read_text(), "new native")

    def test_failed_native_health_restores_previous_runtime(self):
        (self.root / "opt/mesh/mesh-native").write_text("old native")
        self.runtime_selection().write_text("old runtime")
        self.native_release("native")
        self.environment["FAIL_SERVICE"] = "is-active --quiet mesh.service"
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assert_old_install()
        self.assertEqual((self.root / "opt/mesh/mesh-native").read_text(), "old native")
        self.assertEqual(self.runtime_selection().read_text(), "old runtime")

    def test_failed_first_native_install_restores_go(self):
        self.native_release("native")
        self.environment["FAIL_SERVICE"] = "is-active --quiet mesh.service"
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assert_old_install()
        self.assertFalse((self.root / "opt/mesh/mesh-native").exists())
        self.assertFalse(self.runtime_selection().exists())

    def test_native_runtime_requires_a_native_build_and_known_runtime(self):
        for runtime, native in (("native", False), ("rust", True)):
            with self.subTest(runtime=runtime):
                self.native_release(runtime)
                if not native:
                    (self.root / "opt/mesh-src/mesh-native").unlink()
                self.assertNotEqual(self.run_install().returncode, 0)
                self.assert_old_install()
                self.assertFalse((self.root / "services.log").exists())

    def prepare_legacy_rename(self):
        import shutil
        legacy_env = self.root / "etc/daochi/daochi.env"
        legacy_env.parent.mkdir(parents=True)
        legacy_data = self.root / "var/lib/daochi/daochi.db"
        legacy_data.parent.mkdir(parents=True)
        legacy_data.write_bytes(b"original encrypted account data\x00\xff")
        legacy_env.write_text("DAOCHI_DB=" + str(legacy_data) + "\nDAOCHI_TOKEN_SECRET_HEX=stable\n")
        legacy_env.chmod(0o600)
        legacy_binary = self.root / "opt/daochi/daochi"
        legacy_binary.parent.mkdir(parents=True)
        legacy_binary.write_text("old binary")
        (self.root / "etc/daochi/node.key").write_bytes(bytes(range(64)))
        (self.root / "etc/systemd/system/daochi.service").write_text(
            "[Service]\nWorkingDirectory=" + str(legacy_data.parent) +
            "\nEnvironmentFile=/etc/daochi/daochi.env\nExecStart=/opt/daochi/daochi\n")
        # Remove the preexisting Mesh fixture so this exercises the first rename.
        shutil.rmtree(self.root / "opt/mesh")
        shutil.rmtree(self.root / "etc/mesh")
        shutil.rmtree(self.root / "etc/systemd/system/mesh.service.d")
        shutil.copy2(REPOSITORY / "deploy/migrate-install.py", self.root / "opt/mesh-src/deploy/migrate-install.py")
        for name in ("install.sh", "rollback.sh"):
            script = self.root / "opt/mesh-src/deploy" / name
            text = script.read_text().replace(
                "migrate-install.py prepare \"$STAMP\"",
                "migrate-install.py prepare \"$STAMP\" --root \"$TEST_ROOT\"").replace(
                "migrate-install.py restore \"$STAMP\"",
                "migrate-install.py restore \"$STAMP\" --root \"$TEST_ROOT\"")
            script.write_text(text)
        shutil.copy2(REPOSITORY / "scripts/migrate-legacy-env.py", self.root / "opt/mesh-src/scripts/migrate-legacy-env.py")
        self.executable("opt/mesh-src/deploy/database-path.py", 'import os; print(os.environ["TEST_ROOT"] + "/var/lib/daochi/daochi.db")')
        self.executable("bin/sqlite3", "import shutil, shlex, sys; shutil.copyfile(sys.argv[1], shlex.split(sys.argv[2])[1])")
        return legacy_data, legacy_env

    def test_first_rename_then_install_preserves_original_data_keys_and_config(self):
        legacy_data, legacy_env = self.prepare_legacy_rename()
        original = legacy_env.read_bytes()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual((self.root / "opt/mesh/mesh").read_text(), "new binary")
        self.assertEqual(legacy_data.read_bytes(), b"original encrypted account data\x00\xff")
        self.assertEqual(legacy_env.read_bytes(), original)
        self.assertEqual((self.root / "etc/mesh/node.key").read_bytes(), bytes(range(64)))
        env = (self.root / "etc/mesh/mesh.env").read_text()
        self.assertIn("MESH_TOKEN_SECRET_HEX=stable", env)
        self.assertIn("MESH_DB=" + str(legacy_data), env)

    def test_first_rename_failed_migration_restores_original_service_and_data(self):
        legacy_data, legacy_env = self.prepare_legacy_rename()
        original = legacy_env.read_bytes()
        self.environment["FAIL_MIGRATION"] = "1"
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(legacy_env.read_bytes(), original)
        self.assertEqual(legacy_data.read_bytes(), b"original encrypted account data\x00\xff")
        self.assertEqual((self.root / "opt/daochi/daochi").read_text(), "old binary")
        self.assertFalse((self.root / "etc/systemd/system/mesh.service").exists())
        self.assertIn("start daochi.service", (self.root / "services.log").read_text())

    def prepare_sqlite_legacy_store(self):
        database, _ = self.prepare_legacy_rename()
        database.unlink()
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE records(id TEXT PRIMARY KEY, encrypted BLOB)")
            connection.execute("INSERT INTO records VALUES(?, ?)", ("existing-account", bytes(range(256))))
        self.executable("bin/sqlite3", '''
import shlex, sqlite3, sys
destination = shlex.split(sys.argv[2])
assert destination[0] == ".backup"
with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(destination[1]) as target:
    source.backup(target)
''')
        return database

    def assert_sqlite_records(self, database):
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual(connection.execute("SELECT * FROM records").fetchall(), [("existing-account", bytes(range(256)))])

    def test_real_sqlite_snapshot_preserves_encrypted_rows_after_rename(self):
        database = self.prepare_sqlite_legacy_store()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assert_sqlite_records(database)
        self.assert_sqlite_records(self.root / f"var/lib/mesh/backups/mesh.db.{STAMP}")
        self.assertEqual((self.root / "etc/daochi/node.key").read_bytes(), bytes(range(64)))

    def test_real_sqlite_snapshot_restores_rows_after_failed_data_migration(self):
        database = self.prepare_sqlite_legacy_store()
        (self.root / "opt/mesh-src/deploy/apply-dew-ratio.sh").write_text('''
python3 - "$4" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("UPDATE records SET encrypted = ?", (b"partial migration",))
PY
exit 1
''')
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assert_sqlite_records(database)
        self.assertFalse((self.root / "etc/systemd/system/mesh.service").exists())
        self.assertIn("start daochi.service", (self.root / "services.log").read_text())

    def test_json_entry_keeps_credentials_out_of_arguments(self):
        spec = importlib.util.spec_from_file_location("deploy_install", REPOSITORY / "deploy/install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        request = {"stamp": STAMP, "source_commit": "a" * 40, "native_commit": "b" * 40, "mesh_token": "private credential"}
        with mock.patch.object(module.subprocess, "run") as run:
            module.install(request)
            self.assertNotIn(request["mesh_token"], run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["input"], b"private credential")
            self.assertTrue(run.call_args.kwargs["check"])
        with self.assertRaises(ValueError):
            module.install({"stamp": []})


if __name__ == "__main__":
    unittest.main()
