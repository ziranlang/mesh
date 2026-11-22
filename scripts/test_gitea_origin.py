#!/usr/bin/env python3
"""Exercise the origin's Gitea run and artifact gates with local fixtures."""

import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('gitea_origin', Path(__file__).resolve().parents[1] / 'deploy/gitea-origin.py')
origin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(origin)


class OriginPolicyTest(unittest.TestCase):
    revision = '1' * 40

    def setUp(self):
        self.branch = {'commit': {'id': self.revision}}
        self.run = {'id': 42, 'run_attempt': 3, 'head_sha': self.revision, 'head_branch': 'master',
                    'path': 'ci.yml@refs/heads/master', 'event': 'push', 'status': 'in_progress'}
        self.jobs = {'jobs': [{'name': job, 'status': 'completed', 'conclusion': 'success'}
                              for job in ('checks', 'container')] + [{'name': 'deploy', 'status': 'in_progress'}]}
        self.artifacts = {'artifacts': [{'id': 88, 'name': 'mesh-origin-source-3', 'expired': False,
                                        'size_in_bytes': 1593}]}

    def verify(self):
        get = {'/branches/master': self.branch, '/actions/runs/42': self.run,
               '/actions/runs/42/jobs': self.jobs, '/actions/runs/42/artifacts': self.artifacts}.__getitem__
        origin.verify_run(42, 3, self.revision, 88, get)

    def test_canonical_attempt(self):
        self.verify()

    def test_ref_event_attempt_gate_and_artifact_are_bound(self):
        for target, field, value in (
            (self.branch['commit'], 'id', '2' * 40), (self.run, 'event', 'pull_request'),
            (self.run, 'path', 'later.yml@refs/heads/master'), (self.run, 'run_attempt', 2),
            (self.jobs['jobs'][0], 'conclusion', 'failure'),
            (self.jobs['jobs'][-1], 'status', 'completed'),
            (self.artifacts['artifacts'][0], 'name', 'mesh-origin-source-2'),
            (self.artifacts['artifacts'][0], 'expired', True),
        ):
            with self.subTest(field=field):
                old = target[field]
                try:
                    target[field] = value
                    with self.assertRaises(origin.DeployError):
                        self.verify()
                finally:
                    target[field] = old

    def test_every_required_job_must_pass(self):
        for name in ('checks', 'container'):
            with self.subTest(job=name):
                job = next(item for item in self.jobs['jobs'] if item['name'] == name)
                job['conclusion'] = 'failure'
                with self.assertRaises(origin.DeployError):
                    self.verify()
                job['conclusion'] = 'success'

    def test_missing_required_job_is_rejected(self):
        self.jobs['jobs'] = [job for job in self.jobs['jobs'] if job['name'] != 'checks']
        with self.assertRaises(origin.DeployError):
            self.verify()

    def test_forced_command_grammar(self):
        command = f'deploy 42 3 {self.revision} {"4" * 40} {"5" * 64} 6667 88'
        self.assertIsNotNone(origin.COMMAND.fullmatch(command))
        for invalid in (command + ' extra', 'sh', '', command.replace('6667', '-1')):
            self.assertIsNone(origin.COMMAND.fullmatch(invalid))

    def test_archive_requires_only_unique_regular_paths(self):
        for names, kind, expected in ((['src/main.zi'], tarfile.REGTYPE, True),
                                      (['../elsewhere'], tarfile.REGTYPE, False),
                                      (['/elsewhere'], tarfile.REGTYPE, False),
                                      (['a', './a'], tarfile.REGTYPE, False),
                                      (['a'], tarfile.SYMTYPE, False),
                                      (['a'], tarfile.LNKTYPE, False)):
            with self.subTest(names=names, kind=kind):
                tar_bytes = io.BytesIO()
                with tarfile.open(fileobj=tar_bytes, mode='w:gz') as archive:
                    for name in names:
                        member = tarfile.TarInfo(name)
                        member.type = kind
                        member.size = 1 if kind == tarfile.REGTYPE else 0
                        archive.addfile(member, io.BytesIO(b'x'))
                bundle = io.BytesIO()
                with zipfile.ZipFile(bundle, mode='w') as archive:
                    archive.writestr('mesh-source.tar.gz', tar_bytes.getvalue())
                bundle.seek(0)
                with tempfile.TemporaryDirectory() as directory:
                    out = Path(directory)
                    if expected:
                        origin.unpack_source(bundle, out)
                        self.assertEqual((out / names[0]).read_bytes(), b'x')
                    else:
                        with self.assertRaises(origin.DeployError):
                            origin.unpack_source(bundle, out)
                        self.assertEqual(list(out.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
