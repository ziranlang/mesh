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
REQUIRED_JOBS = {"checks", "container"}
SHARED_SLOT_SETUP = ("./.gitea/actions/generate", "./.gitea/actions/liboqs")


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
        self.assertEqual(set(self.jobs), REQUIRED_JOBS | {"deploy"})
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
        self.assertEqual(set(read_yaml(ROOT / ".github/workflows/deploy.yml")["on"]), {"workflow_dispatch"})
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

    def test_shared_slot_sets_up_and_tests_once(self):
        steps = self.jobs["checks"]["steps"]
        uses = [step.get("uses") for step in steps]
        for action in SHARED_SLOT_SETUP:
            self.assertEqual(uses.count(action), 1)
        runs = [step.get("run", "") for step in steps]
        plain_tests = [run for run in runs if re.search(r"make test(\s|$)", run) and "-race" not in run]
        self.assertEqual(len(plain_tests), 1)
        self.assertIn("-coverprofile=coverage.out", plain_tests[0])
        self.assertEqual(sum("-race" in run for run in runs), 1)
        for run in runs:
            if re.search(r"make test(\s|$)", run):
                self.assertIn("-timeout=30m", run)
        coverage = next(step for step in steps if step.get("name") == "Test and report coverage")
        self.assertIn("GOFLAGS=-mod=mod go tool cover", coverage["run"])
        for name in ("Lint", "Race test", "Scan generated Go and check dependency licenses"):
            self.assertIn(name, [step.get("name") for step in steps])
        scans = [run for run in runs if "govulncheck -scan=package . ./native" in run]
        self.assertEqual(len(scans), 2)
        # gosec with -quiet writes no report when it finds nothing, and the upload requires one.
        scan = next(step for step in steps if step.get("name") == "Scan generated Go and check dependency licenses")
        self.assertIn("-fmt=sarif -out gosec.sarif", scan["run"])
        self.assertNotIn("-quiet", scan["run"])
        script_tests = re.findall(r"python3 -B (scripts/test_\w+\.py)", "\n".join(runs))
        self.assertEqual(len(script_tests), len(set(script_tests)))
        setup_go = next(step for step in steps if "actions/setup-go@" in (step.get("uses") or ""))
        self.assertEqual(setup_go["with"]["cache"], "false")
        names = [step.get("name") for step in steps]
        root = names.index("Use the installed Go root")
        self.assertEqual(root, steps.index(setup_go) + 1)
        self.assertIn('echo "GOROOT=$root" >> "$GITHUB_ENV"', steps[root]["run"])

    def test_make_shim_forwards_every_target_ci_and_the_origin_run(self):
        shim = (ROOT / "makefile").read_text()
        forwarded = set(re.search(r"^build test run[^\n]*:", shim, re.MULTILINE).group(0).rstrip(":").split())
        runs = "\n".join(step.get("run", "") for job in self.jobs.values() for step in job["steps"])
        targets = set(re.findall(r"\bmake ((?:[a-z][\w-]*(?: |$))+)", runs, re.MULTILINE))
        wanted = {word for group in targets for word in group.split() if "=" not in word}
        wanted |= {"build", "build-native"}
        self.assertLessEqual(wanted, forwarded | {"all"})

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
