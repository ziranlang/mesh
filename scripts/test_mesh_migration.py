"""Exercise real filesystem migration with opaque keys and an existing database."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import yaml
from unittest.mock import patch
from subprocess import CompletedProcess


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "deploy" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = load("installed_migration", "migrate-install.py")
database = load("database_path", "database-path.py")
STAMP = "20261117T000000Z"


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "build/test-tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = {
            "etc/daochi/daochi.env": b"DAOCHI_TOKEN_SECRET_HEX=existing\nDAOCHI_DB=/var/lib/daochi/daochi.db\n",
            "etc/daochi/node.key": bytes(range(64)),
            "opt/daochi/daochi": b"original executable",
            "var/lib/daochi/daochi.db": bytes(range(256)),
            "var/lib/daochi/daochi.db.node-key": bytes(range(32)),
            "var/lib/daochi/blobs/object": b"encrypted\0payload\xff",
            "etc/systemd/system/daochi.service": b"[Service]\nUser=meshowner\nWorkingDirectory=/var/lib/daochi\nEnvironmentFile=/etc/daochi/daochi.env\nExecStart=/opt/daochi/daochi\n",
        }
        for name, value in self.files.items():
            path = self.root / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_bytes(value)
            path.chmod(0o600)
        self.calls = []
        self.active = True
        self.patch = patch.object(migration, "systemctl", self.systemctl)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def systemctl(self, *args, **kwargs):
        self.calls.append(args)
        if args == ("is-active", "--quiet", "daochi.service"):
            return CompletedProcess(args, 0 if self.active else 3, "", "")
        if args == ("stop", "daochi.service"):
            self.active = False
        if args == ("start", "daochi.service"):
            self.active = True
        return CompletedProcess(args, 0, "", "")

    def test_migrate_preserves_all_original_bytes_identity_and_ownership(self):
        migration.prepare(self.root, STAMP)
        for name, value in self.files.items():
            self.assertEqual((self.root / name).read_bytes(), value)
        key = self.root / "etc/mesh/node.key"
        self.assertEqual(key.read_bytes(), bytes(range(64)))
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.stat().st_uid, (self.root / "etc/daochi/node.key").stat().st_uid)
        unit = (self.root / "etc/systemd/system/mesh.service").read_text()
        self.assertIn("WorkingDirectory=/var/lib/daochi", unit)
        self.assertIn("ExecStart=/opt/mesh/mesh", unit)
        environment = (self.root / "etc/mesh/mesh.env").read_text()
        self.assertEqual(database.database_path(environment, "/var/lib/daochi"), "/var/lib/daochi/daochi.db")
        calls = list(self.calls)
        migration.prepare(self.root, STAMP)
        self.assertEqual(self.calls[len(calls):], [("is-active", "--quiet", "daochi.service")])
        migration.restore(self.root, STAMP)
        self.assertFalse((self.root / "etc/systemd/system/mesh.service").exists())
        self.assertIn(("start", "daochi.service"), self.calls)
        migration.prepare(self.root, STAMP)
        self.assertTrue((self.root / "etc/systemd/system/mesh.service").exists())

    def test_unset_database_is_pinned_and_conflicts_refused(self):
        (self.root / "etc/daochi/daochi.env").write_text("DAOCHI_TOKEN_SECRET_HEX=existing\n")
        migration.prepare(self.root, STAMP)
        self.assertEqual(database.database_path((self.root / "etc/mesh/mesh.env").read_text(), "/var/lib/daochi"), "/var/lib/daochi/daochi.db")

    def test_relative_database_remains_relative_to_original_working_directory(self):
        self.assertEqual(database.database_path('DAOCHI_DB="existing state.db"\n', "/var/lib/daochi"), "/var/lib/daochi/existing state.db")

    def test_unit_rename_never_rewrites_database_key_or_argument_paths(self):
        original = (
            "[Service]\n"
            "EnvironmentFile=-\"/etc/daochi/daochi.env\"\n"
            "Environment=DAOCHI_DB=/opt/daochi/daochi.db\n"
            "Environment=DAOCHI_NODE_IDENTITY_KEY_FILE=/opt/daochi/daochi.key\n"
            "WorkingDirectory=/opt/daochi\n"
            "ExecStart=/opt/daochi/daochi --db /opt/daochi/daochi.db\n"
            'ExecStartPre=-"/opt/daochi/daochi" inspect /opt/daochi/daochi.db\n'
        )
        expected = original.replace(
            'EnvironmentFile=-"/etc/daochi/daochi.env"',
            'EnvironmentFile=-"/etc/mesh/mesh.env"',
        ).replace("ExecStart=/opt/daochi/daochi ", "ExecStart=/opt/mesh/mesh ")
        expected = expected.replace('ExecStartPre=-"/opt/daochi/daochi"', 'ExecStartPre=-"/opt/mesh/mesh"')
        self.assertEqual(migration.renamed_unit(original), expected)

    def test_refuses_existing_mesh_binary_before_stopping_service(self):
        (self.root / "opt/mesh").mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            migration.prepare(self.root, STAMP)
        self.assertEqual(self.calls, [])

    def test_default_store_inside_old_install_is_never_copied_or_moved(self):
        old_store = self.root / "opt/daochi/daochi.db"
        old_store.write_bytes(b"live encrypted database")
        migration.prepare(self.root, STAMP)
        self.assertEqual(old_store.read_bytes(), b"live encrypted database")
        self.assertEqual(sorted(p.name for p in (self.root / "opt/mesh").iterdir()), ["mesh"])

    def test_symlink_configuration_is_refused_without_touching_original(self):
        old = self.root / "etc/daochi/daochi.env"
        target = old.with_name("original.env")
        old.rename(target)
        old.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink roots"):
            migration.prepare(self.root, STAMP)
        self.assertEqual(target.read_bytes(), self.files["etc/daochi/daochi.env"])
        self.assertEqual(self.calls, [])

    def test_database_in_config_directory_is_never_copied(self):
        store = self.root / "etc/daochi/accounts"
        original = b"SQLite format 3\0" + bytes(range(64))
        store.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "contains a database"):
            migration.prepare(self.root, STAMP)
        self.assertEqual(store.read_bytes(), original)
        self.assertFalse((self.root / "etc/mesh").exists())
        self.assertEqual(self.calls, [])

    def test_existing_mesh_config_does_not_allow_a_second_database_writer(self):
        migration.prepare(self.root, STAMP)
        self.active = True
        with self.assertRaisesRegex(ValueError, "still running"):
            migration.prepare(self.root, STAMP)

    def test_compose_requires_the_existing_volume_and_database(self):
        root = Path(__file__).resolve().parents[1]
        compose = yaml.safe_load((root / "docker-compose.yml").read_text())
        self.assertTrue(compose["volumes"]["mesh-data"]["external"])
        self.assertEqual(compose["volumes"]["mesh-data"]["name"], "${MESH_DATA_VOLUME:-daochi_daochi-data}")
        self.assertEqual(compose["services"]["mesh"]["environment"]["MESH_DB"], "/data/daochi.db")


if __name__ == "__main__":
    unittest.main()
