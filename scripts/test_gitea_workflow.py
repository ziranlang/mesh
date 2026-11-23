#!/usr/bin/env python3
"""Check the Gitea deployment graph, pinned inputs, and runner shell scripts."""

from pathlib import Path
import re
import subprocess
import unittest

import yaml

from verify_build_inputs import EXPECTED_GOLANGCI_LINT_SHA256, EXPECTED_GOLANGCI_LINT_VERSION


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".gitea/workflows/ci.yml"
ACTION_REF = re.compile(r"^https://(?:github\.com|git\.waozi\.xyz)/[\w./-]+@[a-f0-9]{40}$")
REQUIRED_JOBS = {"checks", "native", "scans"}
SHARED_SLOT_SETUP = ("./.gitea/actions/generate", "./.gitea/actions/liboqs")
PUSH = "github.event_name != 'schedule'"
WEEKLY = "github.event_name == 'schedule'"


def read_yaml(path):
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


class GiteaWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = read_yaml(WORKFLOW)
        self.jobs = self.workflow["jobs"]

    def test_canonical_deploy_requires_every_gate(self):
        self.assertEqual(self.workflow["on"]["push"]["branches"], ["master"])
        self.assertEqual(self.workflow["on"]["push"]["paths-ignore"], ["plans/**", "**/*.md"])
        self.assertEqual(set(self.workflow["on"]), {"push", "workflow_dispatch", "schedule"})
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        deploy = self.jobs["deploy"]
        self.assertEqual(set(deploy["needs"]), REQUIRED_JOBS)
        self.assertEqual(set(self.jobs), REQUIRED_JOBS | {"deploy", "container"})
        # Nothing deploys the container image, so only the weekly run builds it.
        self.assertEqual(self.jobs["container"]["if"], WEEKLY)
        for guard in (
            # Gitea evaluates job conditions with its ROOT_URL, which ends in a slash.
            "(github.server_url == 'https://git.waozi.xyz' || github.server_url == 'https://git.waozi.xyz/')",
            "github.repository == 'ziranlang/mesh'",
            "github.ref == 'refs/heads/master'",
            "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')",
            "vars.MESH_DEPLOY_READY == 'true'",
            "vars.MESH_DEPLOY_RUNNER != ''",
        ):
            self.assertIn(guard, deploy["if"])
        self.assertNotIn("environment", deploy)
        self.assertEqual(deploy["permissions"], {"contents": "read", "actions": "read"})
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], "false")
        # Gitea is the only deploy path; the GitHub mirror holds no origin key.
        for retired in ("deploy.yml", "bootstrap-ssh.yml"):
            self.assertFalse((ROOT / ".github/workflows" / retired).exists())
        for filename in ("ci.yml", "security.yml"):
            self.assertEqual(set(read_yaml(ROOT / ".github/workflows" / filename)["on"]), {"workflow_call"})

    def test_origin_gate_matches_the_deploy_needs(self):
        origin = (ROOT / "deploy/gitea-origin.py").read_text()
        match = re.search(r"^REQUIRED_JOBS = (\{.*?\})$", origin, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match)
        self.assertEqual(eval(match.group(1), {}), set(self.jobs["deploy"]["needs"]))
        # Gitea reports matrix jobs under suffixed names the origin would not match.
        for job_id in REQUIRED_JOBS:
            self.assertNotIn("strategy", self.jobs[job_id])

    def test_each_push_runs_the_go_tests_once(self):
        steps = self.jobs["checks"]["steps"]
        uses = [step.get("uses") for step in steps]
        for action in SHARED_SLOT_SETUP:
            self.assertEqual(uses.count(action), 1)
        tests = [step for step in steps if re.search(r"make test(\s|$)", step.get("run", ""))]
        for step in tests:
            self.assertIn("-timeout=30m", step["run"])
        per_push = [step for step in tests if step.get("if") == PUSH]
        self.assertEqual(len(per_push), 1)
        self.assertNotIn("-race", per_push[0]["run"])
        self.assertNotIn("-coverprofile", per_push[0]["run"])
        weekly = {step["name"]: step["run"] for step in tests if step.get("if") == WEEKLY}
        self.assertEqual(set(weekly), {"Test and report coverage", "Race test"})
        self.assertIn("GOFLAGS=-mod=mod go tool cover", weekly["Test and report coverage"])
        self.assertIn("-race", weekly["Race test"])
        self.assertEqual(len(tests), 3)
        self.assertIn("Lint", [step.get("name") for step in steps])
        runs = [step.get("run", "") for job in REQUIRED_JOBS for step in self.jobs[job]["steps"]]
        script_tests = re.findall(r"python3 -B (scripts/test_\w+\.py)", "\n".join(runs))
        self.assertEqual(len(script_tests), len(set(script_tests)))
        for job in REQUIRED_JOBS:
            job_steps = self.jobs[job]["steps"]
            setup_go = next(step for step in job_steps if "actions/setup-go@" in (step.get("uses") or ""))
            self.assertEqual(setup_go["with"]["cache"], "false")
            names = [step.get("name") for step in job_steps]
            root = names.index("Use the installed Go root")
            self.assertEqual(root, job_steps.index(setup_go) + 1)
            self.assertIn('echo "GOROOT=$root" >> "$GITHUB_ENV"', job_steps[root]["run"])

    def test_native_suite_reuses_the_kept_libraries(self):
        steps = self.jobs["native"]["steps"]
        uses = [step.get("uses") for step in steps]
        self.assertLess(uses.index("./.gitea/actions/ziran"), uses.index("./.gitea/actions/native-libraries"))
        native = next(step for step in steps if step.get("name") == "Native server suite")
        self.assertIn("make test-ziran-native-server", native["run"])
        self.assertLess(uses.index("./.gitea/actions/native-libraries"), steps.index(native))

    def test_scans_run_once_per_set_of_inputs(self):
        steps = self.jobs["scans"]["steps"]
        inputs = next(step for step in steps if step.get("name") == "Find the scanned inputs")
        self.assertIn('[ "$EVENT" != schedule ]', inputs["run"])
        for step in steps[steps.index(inputs) + 1:]:
            self.assertIn("steps.inputs.outputs.unchanged != 'true'", step["if"])
        runs = [step.get("run", "") for step in steps]
        self.assertEqual(sum("govulncheck -scan=package . ./native" in run for run in runs), 2)
        # gosec with -quiet writes no report when it finds nothing, and the upload requires one.
        scan = next(step for step in steps if step.get("name") == "Scan generated Go and check dependency licenses")
        self.assertIn("-fmt=sarif -out gosec.sarif", scan["run"])
        self.assertNotIn("-quiet", scan["run"])
        self.assertEqual(steps[-1]["name"], "Record the scanned inputs")

    def test_make_shim_forwards_every_target_ci_and_the_origin_run(self):
        shim = (ROOT / "makefile").read_text()
        forwarded = set(re.search(r"^build test run[^\n]*:", shim, re.MULTILINE).group(0).rstrip(":").split())
        runs = "\n".join(step.get("run", "") for job in self.jobs.values() for step in job["steps"])
        targets = set(re.findall(r"\bmake ((?:[a-z][\w-]*(?: |$))+)", runs, re.MULTILINE))
        wanted = {word for group in targets for word in group.split() if "=" not in word}
        wanted |= {"build", "build-native"}
        self.assertLessEqual(wanted, forwarded | {"all"})

    def test_deploy_ships_the_binaries_ci_built(self):
        uploads = {}
        for job in ("checks", "native"):
            for step in self.jobs[job]["steps"]:
                if "actions/upload-artifact@" in (step.get("uses") or ""):
                    self.assertEqual(step["if"], PUSH)
                    uploads[step["with"]["name"]] = step["with"]["path"].split()
        self.assertEqual(uploads, {"mesh-go-release": ["mesh", "build/mesh.manifest.json"],
                                   "mesh-native-release": ["mesh-native", "build/mesh-native.manifest.json"]})
        steps = self.jobs["deploy"]["steps"]
        downloads = [step["with"]["name"] for step in steps if "actions/download-artifact@" in (step.get("uses") or "")]
        self.assertEqual(set(downloads), set(uploads))
        # Nothing builds in the deploy: it packages what checks and native built.
        self.assertFalse(any("actions/setup-go@" in (step.get("uses") or "") for step in steps))
        self.assertFalse(any("make " in step.get("run", "") for step in steps))
        package = next(step for step in steps if step.get("name") == "Package the exact canonical revision")
        for path in (path for paths in uploads.values() for path in paths):
            self.assertIn(path, package["run"])

    def test_container_probe_stays_inside_its_network_namespace(self):
        steps = self.jobs["container"]["steps"]
        verify = next(step for step in steps if step.get("name") == "Verify container starts as a non-root user")
        self.assertNotIn(" -p ", verify["run"])
        self.assertIn('--network "container:$container"', verify["run"])
        self.assertRegex(verify["env"]["PROBE_IMAGE"], r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$")

    def test_generated_go_reuses_the_runner_package_cache(self):
        steps = read_yaml(ROOT / ".gitea/actions/generate/action.yml")["runs"]["steps"]
        generate = next(step for step in steps if step.get("name") == "Generate the Go")
        self.assertNotIn("XDG_CACHE_HOME", generate.get("env", {}))
        self.assertNotIn("runner.temp", str(generate))
        env = generate["env"]
        rewrites = {env[f"GIT_CONFIG_VALUE_{index}"]: env[f"GIT_CONFIG_KEY_{index}"]
                    for index in range(int(env["GIT_CONFIG_COUNT"]))}
        for organization in ("ziranlang", "kryonlabs"):
            self.assertEqual(rewrites[f"https://github.com/{organization}/"],
                             f"url.https://git.waozi.xyz/{organization}/.insteadOf")

    def test_pinned_actions_and_job_shell_syntax(self):
        step_groups = [job["steps"] for job in self.jobs.values()]
        for action_file in (ROOT / ".gitea/actions").glob("*/action.yml"):
            step_groups.append(read_yaml(action_file)["runs"]["steps"])
        for steps in step_groups:
            for step in steps:
                action = step.get("uses")
                if action and not action.startswith("./"):
                    self.assertRegex(action, ACTION_REF)
                if "actions/checkout@" in (action or ""):
                    self.assertEqual(step["with"]["persist-credentials"], "false")
                    self.assertEqual(step["with"]["submodules"], "recursive")
                if "run" in step:
                    with self.subTest(step=step.get("name")):
                        bash = re.sub(r"\$\{\{.*?\}\}", "0" * 40, step["run"])
                        subprocess.run(["bash", "-n"], text=True, input=bash, check=True)
        container = self.jobs["container"]["steps"]
        build = next(step for step in container if step.get("name") == "Build container image")
        scan = next(step for step in container if step.get("name") == "Scan image")
        self.assertIn('mesh:ci-$GITHUB_RUN_ID', build["run"])
        self.assertEqual(scan["with"]["image-ref"], "mesh:ci-${{ github.run_id }}")
        self.assertEqual(scan["with"]["exit-code"], "1")
        self.assertEqual(scan["with"]["severity"], "CRITICAL,HIGH")
        self.assertEqual(scan["with"]["skip-setup-trivy"], "true")
        install = next(step for step in container if step.get("name") == "Install the pinned image scanner")
        self.assertLess(container.index(install), container.index(scan))
        self.assertRegex(install["env"]["TRIVY_SHA256"], r"^[0-9a-f]{64}$")
        self.assertLess(install["run"].index("sha256sum --check --strict"), install["run"].index("tar --extract"))
        source = WORKFLOW.read_text()
        for job_id in REQUIRED_JOBS:
            self.assertNotRegex(str(self.jobs[job_id]), r"\$\{\{\s*secrets\.")
        self.assertNotIn("ssh-keyscan", source)
        self.assertNotIn("~/.ssh", source)
        deploy = self.jobs["deploy"]
        origin = next(step for step in deploy["steps"] if step.get("name") == "Deploy the verified Gitea artifact")
        self.assertIn("printf '%s' \"$GITEA_JOB_TOKEN\" | ssh", origin["run"])
        self.assertIn('deploy "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$GITHUB_SHA"', origin["run"])
        self.assertNotIn("/opt/mesh-src", source)

    def test_linter_pins_and_checksum_are_retained(self):
        step = next(item for item in self.jobs["checks"]["steps"] if item.get("name") == "Install golangci-lint")
        self.assertEqual(step["env"], {
            "GOLANGCI_LINT_VERSION": EXPECTED_GOLANGCI_LINT_VERSION,
            "GOLANGCI_LINT_SHA256": EXPECTED_GOLANGCI_LINT_SHA256,
        })
        self.assertIn("sha256sum --check --strict", step["run"])
        self.assertLess(step["run"].index("sha256sum"), step["run"].index("tar --extract"))


if __name__ == "__main__":
    unittest.main()
