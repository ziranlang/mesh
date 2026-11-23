import importlib.util
import json
import re
from hashlib import sha256
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml


script_path = Path(__file__).with_name("verify_deployment_inputs.py")
spec = importlib.util.spec_from_file_location("deployment_inputs", script_path)
deployment_inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment_inputs)


class DeploymentInputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Path("build").mkdir(exist_ok=True)

    def run_git(self, repository, *arguments):
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def write_host_key(self, directory, name):
        key_path = directory / name
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                name,
                "-f",
                str(key_path),
            ],
            check=True,
        )
        return f"deployment.test {key_path.with_suffix('.pub').read_text().strip()}"

    def test_pinned_source_rejects_a_wrong_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            source = repository / "vendor" / "liboqs"
            source.mkdir(parents=True)
            (source / "CMakeLists.txt").write_text("first\n")
            self.run_git(source, "init")
            self.run_git(source, "config", "user.name", "Fixture")
            self.run_git(source, "config", "user.email", "fixture@example.test")
            self.run_git(source, "add", ".")
            self.run_git(source, "commit", "-m", "first")
            self.run_git(repository, "init")
            self.run_git(repository, "config", "user.name", "Fixture")
            self.run_git(repository, "config", "user.email", "fixture@example.test")
            self.run_git(
                repository,
                "update-index",
                "--add",
                "--cacheinfo",
                "160000,d95fc734f5de9bdfc80c8c65d8166b30e9cff5cd,vendor/liboqs",
            )
            self.run_git(repository, "commit", "--allow-empty", "-m", "fixture")
            with self.assertRaisesRegex(
                deployment_inputs.VerificationError,
                "gitlink is d95fc73.* but checkout is",
            ):
                deployment_inputs.pinned_source(repository, "vendor/liboqs")

    def test_source_verification_rejects_a_one_byte_difference(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "input.txt").write_bytes(b"expected")
            identity = deployment_inputs.source_identity(source)
            deployment_inputs.verify_source(
                source, "0" * 40, identity["digest"], identity["entries"]
            )
            (source / "input.txt").write_bytes(b"rejected")
            with self.assertRaisesRegex(
                deployment_inputs.VerificationError,
                "liboqs source identity mismatch",
            ):
                deployment_inputs.verify_source(
                    source, "0" * 40, identity["digest"], identity["entries"]
                )

    def test_stale_native_cache_is_rejected(self):
        source = Path("vendor/liboqs")
        with tempfile.TemporaryDirectory(
            prefix="deployment-inputs-", dir="build"
        ) as temporary:
            build = Path(temporary)
            artifact = build / "install" / "lib" / "liboqs.a"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"native artifact")
            options = "-DOQS_MINIMAL_BUILD=SIG_ml_dsa_44"
            deployment_inputs.validate_native_paths(source, build)
            deployment_inputs.record_native_build(source, build, artifact, "cmake", options)
            self.assertTrue(
                deployment_inputs.check_native_build(source, build, artifact, "cmake", options)
            )
            self.assertFalse(
                deployment_inputs.check_native_build(
                    source, build, artifact, "cmake", "-DOQS_MINIMAL_BUILD=wrong"
                )
            )
            self.assertFalse(
                deployment_inputs.check_native_build(
                    source, build, Path("/missing"), "cmake", options
                )
            )
            artifact.write_bytes(b"changed native artifact")
            self.assertFalse(
                deployment_inputs.check_native_build(source, build, artifact, "cmake", options)
            )
            artifact.write_bytes(b"native artifact")
            metadata_path = deployment_inputs.native_metadata_path(build)
            recorded = json.loads(metadata_path.read_text())
            recorded["identity"]["source"]["digest"] = "0" * 64
            metadata_path.write_text(json.dumps(recorded))
            self.assertFalse(
                deployment_inputs.check_native_build(source, build, artifact, "cmake", options)
            )

    def test_artifact_manifest_binds_the_output_digest(self):
        source = Path("vendor/liboqs")
        with tempfile.TemporaryDirectory(
            prefix="deployment-manifest-", dir="build"
        ) as temporary:
            build = Path(temporary)
            native_artifact = build / "liboqs.a"
            native_artifact.write_bytes(b"native artifact")
            native_metadata = build / "native.json"
            output = build / "mesh"
            output.write_bytes(b"deployment artifact")
            manifest = build / "manifest.json"
            native_identity = deployment_inputs.native_build_identity(
                source, "cmake", "-DOQS_MINIMAL_BUILD=SIG_ml_dsa_44"
            )
            native_metadata.write_text(json.dumps(native_identity))

            def fixture_command(arguments, error):
                if len(arguments) == 2:
                    return "go1.26.10 fixture"
                return "main BuildVersion=fixture"

            with mock.patch.object(
                deployment_inputs, "run_command", side_effect=fixture_command
            ):
                provenance = deployment_inputs.record_artifact_manifest(
                    output,
                    manifest,
                    "a" * 40,
                    "b" * 40,
                    native_metadata,
                )
            self.assertEqual(
                provenance["artifact"]["sha256"],
                sha256(b"deployment artifact").hexdigest(),
            )
            deployment_inputs.verify_artifact_manifest(
                output, manifest, "a" * 40, "b" * 40
            )
            recorded = json.loads(manifest.read_text())
            self.assertIn("go.sum", recorded["source_inputs"])
            self.assertIn("main BuildVersion=fixture", recorded["go"]["version_m"])
            output.write_bytes(b"replaced artifact")
            with self.assertRaisesRegex(
                deployment_inputs.VerificationError,
                "artifact digest mismatch",
            ):
                deployment_inputs.verify_artifact_manifest(
                    output, manifest, "a" * 40, "b" * 40
                )

    def test_native_server_manifest_records_the_compiled_sources(self):
        with tempfile.TemporaryDirectory(prefix="native-manifest-", dir="build") as temporary:
            build = Path(temporary)
            sources = build / "native-release"
            (sources / "server").mkdir(parents=True)
            (sources / "server" / "entry.c").write_bytes(b"int main(void) { return 0; }\n")
            native_metadata = build / "native.json"
            native_metadata.write_text(json.dumps(deployment_inputs.native_build_identity(
                Path("vendor/liboqs"), "cmake", "-DOQS_MINIMAL_BUILD=SIG_ml_dsa_44")))
            output = build / "mesh-native"
            output.write_bytes(b"native server")
            manifest = build / "manifest.json"
            with mock.patch.object(deployment_inputs, "run_command", return_value="cc (Debian 14.2.0-19) 14.2.0\nCopyright"):
                provenance = deployment_inputs.record_artifact_manifest(
                    output, manifest, "a" * 40, "b" * 40, native_metadata, "c", sources)
            self.assertNotIn("go", provenance)
            self.assertEqual(provenance["c"]["compiler"], "cc (Debian 14.2.0-19) 14.2.0")
            self.assertEqual(provenance["c"]["sources"], 1)
            first = provenance["c"]["sources_sha256"]
            (sources / "server" / "entry.c").write_bytes(b"int main(void) { return 1; }\n")
            self.assertNotEqual(deployment_inputs.source_tree_digest(sources)[0], first)
            deployment_inputs.verify_artifact_manifest(output, manifest, "a" * 40, "b" * 40)
            with self.assertRaisesRegex(deployment_inputs.VerificationError, "release sources are required"):
                deployment_inputs.record_artifact_manifest(output, manifest, "a" * 40, "b" * 40, native_metadata, "c")

    def test_native_cleanup_rejects_unsafe_paths(self):
        with self.assertRaisesRegex(
            deployment_inputs.VerificationError,
            "native build directory must be inside build/",
        ):
            deployment_inputs.validate_native_paths(
                Path("vendor/liboqs"), Path("/temporary-mesh-build")
            )
        with self.assertRaisesRegex(
            deployment_inputs.VerificationError,
            "native source must be vendor/liboqs",
        ):
            deployment_inputs.validate_native_paths(Path("vendor"), Path("build/liboqs"))

    def test_missing_trusted_host_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            ssh_directory = Path(temporary) / ".ssh"
            with self.assertRaisesRegex(
                deployment_inputs.VerificationError,
                "trusted SSH host key is empty",
            ):
                deployment_inputs.configure_ssh(
                    "deployment.test", "deploy", [], ssh_directory
                )
            self.assertFalse((ssh_directory / "known_hosts").exists())

    def test_ssh_configuration_reuses_existing_key_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trusted = self.write_host_key(root, "host")
            ssh_directory = root / ".ssh"
            ssh_directory.mkdir(mode=0o755)
            identity = ssh_directory / "id_ed25519"
            identity.write_text("existing deployment identity fixture")
            identity.chmod(0o600)
            for _ in range(2):
                deployment_inputs.configure_ssh("deployment.test", "deploy", [trusted], ssh_directory)
            self.assertEqual(identity.read_text(), "existing deployment identity fixture")
            self.assertEqual(ssh_directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((ssh_directory / "known_hosts").stat().st_mode & 0o777, 0o600)
            self.assertIn("StrictHostKeyChecking yes", (ssh_directory / "config").read_text())

    def test_custom_ssh_directory_keeps_identity_and_host_keys_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trusted = self.write_host_key(root, "host")
            ssh_directory = root / "job ssh"
            deployment_inputs.configure_ssh(
                "deployment.test", "deploy", [trusted], ssh_directory
            )
            configuration = subprocess.check_output(
                ["ssh", "-G", "-F", str(ssh_directory / "config"), "mesh-origin"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            options = dict(row.split(" ", 1) for row in configuration.splitlines())
            self.assertEqual(options["identityfile"], str(ssh_directory / "id_ed25519"))
            self.assertEqual(options["userknownhostsfile"], str(ssh_directory / "known_hosts"))
            self.assertEqual(options["stricthostkeychecking"], "true")

    def test_presented_host_key_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            trusted = self.write_host_key(Path(temporary), "trusted")
            presented = self.write_host_key(Path(temporary), "presented")
            deployment_inputs.verify_presented_key(
                "deployment.test", [trusted], [trusted]
            )
            with self.assertRaisesRegex(
                deployment_inputs.VerificationError,
                "presented SSH host key does not match",
            ):
                deployment_inputs.verify_presented_key(
                    "deployment.test", [trusted], [presented]
                )

    def test_certificate_authority_host_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            trusted = self.write_host_key(Path(temporary), "trusted")
            authority = f"@cert-authority {trusted}"
            ssh_directory = Path(temporary) / ".ssh"
            with self.assertRaisesRegex(
                deployment_inputs.VerificationError,
                "trusted SSH host key entries must be literal host keys",
            ):
                deployment_inputs.configure_ssh(
                    "deployment.test", "deploy", [authority], ssh_directory
                )
            self.assertFalse((ssh_directory / "known_hosts").exists())

    def test_workflow_uses_exact_native_source_and_strict_ssh(self):
        workflow_path = Path(".gitea/workflows/ci.yml")
        workflow = yaml.safe_load(workflow_path.read_text())
        workflow_text = workflow_path.read_text()
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "run" not in step:
                    continue
                script = re.sub(r"\$\{\{.*?\}\}", "0" * 40, step["run"])
                subprocess.run(
                    ["bash", "-n"],
                    input=script,
                    text=True,
                    check=True,
                )
        deploy = workflow["jobs"]["deploy"]
        checkouts = deploy["steps"]
        deploy_checkout = next(
            step for step in checkouts if "actions/checkout" in str(step.get("uses"))
        )
        self.assertEqual(deploy_checkout.get("with"), {"persist-credentials": False, "submodules": "recursive"})
        self.assertNotIn("ssh-keyscan", workflow_text)
        self.assertNotIn("--exclude='vendor/liboqs'", workflow_text)
        self.assertIn("git ls-files -z --recurse-submodules", workflow_text)
        self.assertIn("tar --null --no-recursion --files-from=", workflow_text)
        self.assertIn("*.node-key", Path(".dockerignore").read_text().splitlines())
        self.assertNotIn('cp -a "/opt/mesh-src/$keep"', workflow_text)
        step_names = [step.get("name") for step in checkouts]
        self.assertLess(
            step_names.index("Verify the pinned native source"),
            step_names.index("Configure SSH"),
        )
        ssh_commands = [
            line.strip()
            for line in workflow_text.splitlines()
            if re.search(r"(^|\| )ssh ", line.strip())
        ]
        self.assertTrue(ssh_commands)
        self.assertTrue(all('ssh -F "$MESH_SSH_DIR/config" mesh-origin' in line for line in ssh_commands))
        deployment = Path(__file__).resolve().parents[1] / "deploy"
        self.assertIn("deploy/rollback.sh", (deployment / "gitea-origin.py").read_text())
        self.assertIn("trap finish 0", (deployment / "install.sh").read_text())
        self.assertIn("rm -f /opt/mesh/mesh.manifest.json", (deployment / "rollback.sh").read_text())


if __name__ == "__main__":
    unittest.main()
