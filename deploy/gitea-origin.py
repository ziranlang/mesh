#!/usr/bin/python3 -I
"""Allow the Mesh deployment key to install an artifact from a verified Gitea run."""

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ORIGIN = 'https://git.waozi.xyz'
API = ORIGIN + '/api/v1/repos/ziranlang/mesh'
PUBLIC = 'https://api.waozi.xyz'
SOURCE = Path('/opt/mesh-src')
REQUIRED_JOBS = {'checks', 'container'}
MAX_ARCHIVE = 64 * 1024**2
MAX_SOURCE = 512 * 1024**2
COMMAND = re.compile(
    r'deploy ([1-9][0-9]{0,15}) ([1-9][0-9]{0,5}) ([0-9a-f]{40}) ([0-9a-f]{40}) '
    r'([0-9a-f]{64}) ([1-9][0-9]{0,4}) ([1-9][0-9]{0,15})', re.ASCII,
)
TOKEN = re.compile(rb'[A-Za-z0-9._~+/-]{20,4096}', re.ASCII)


class DeployError(Exception):
    pass


class SameOrigin(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl)[:2] != ('https', 'git.waozi.xyz'):
            raise DeployError('Gitea redirected to an unapproved origin')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), SameOrigin())


def open_url(url, token=None, timeout=12):
    request = urllib.request.Request(url, headers={'User-Agent': 'curl/8.14.1', 'Cache-Control': 'no-cache'})
    if token:
        request.add_unredirected_header('Authorization', 'token ' + token)
    return OPENER.open(request, timeout=timeout)


def get_api(path, token):
    with open_url(API + path, token) as response:
        body = response.read(1024**2 + 1)
    if len(body) > 1024**2:
        raise DeployError('Gitea metadata exceeded its limit')
    return json.loads(body)


def require_master(revision, get):
    if get('/branches/master')['commit']['id'] != revision:
        raise DeployError('Requested revision is no longer canonical master')


def verify_run(run_id, attempt, revision, artifact_id, get):
    require_master(revision, get)
    run = get(f'/actions/runs/{run_id}')
    if (run['id'] != run_id or run['run_attempt'] != attempt or run['head_sha'] != revision
            or run['path'] != 'ci.yml@refs/heads/master' or run['head_branch'] != 'master'
            or run['event'] not in {'push', 'workflow_dispatch'} or run['status'] != 'in_progress'):
        raise DeployError('Run is not the active canonical Mesh CI attempt')
    jobs = get(f'/actions/runs/{run_id}/jobs')['jobs']
    succeeded = {job['name'] for job in jobs if job['status'] == 'completed' and job.get('conclusion') == 'success'}
    active = {job['name'] for job in jobs if job['status'] == 'in_progress'}
    if not REQUIRED_JOBS <= succeeded or 'deploy' not in active:
        raise DeployError('Mesh deployment gates have not succeeded')
    artifacts = get(f'/actions/runs/{run_id}/artifacts')['artifacts']
    expected_name = f'mesh-origin-source-{attempt}'
    matches = [item for item in artifacts if item['id'] == artifact_id and item['name'] == expected_name]
    if len(matches) != 1 or matches[0]['expired'] or not 0 < matches[0]['size_in_bytes'] <= MAX_ARCHIVE:
        raise DeployError('Missing usable Mesh source artifact from the active attempt')


def copy_limited(source, target, limit):
    total = 0
    while block := source.read(1024**2):
        total += len(block)
        if total > limit:
            raise DeployError('Archive exceeded its size limit')
        target.write(block)


def unpack_source(bundle, destination):
    with zipfile.ZipFile(bundle) as archive:
        items = archive.infolist()
        if len(items) != 1 or items[0].filename != 'mesh-source.tar.gz' or items[0].file_size > MAX_ARCHIVE:
            raise DeployError('Expected exactly one bounded Mesh source archive')
        with archive.open(items[0]) as member, tempfile.TemporaryFile(dir=destination) as compressed:
            copy_limited(member, compressed, MAX_ARCHIVE)
            compressed.seek(0)
            with tarfile.open(fileobj=compressed, mode='r:gz') as content:
                members = []
                paths = set()
                size = 0
                for entry in content:
                    path = PurePosixPath(entry.name)
                    size += entry.size
                    if (path.is_absolute() or '..' in path.parts or not path.parts or path in paths
                            or not (entry.isfile() or entry.isdir()) or size > MAX_SOURCE or len(members) >= 10000):
                        raise DeployError('Mesh archive contains an unsafe member')
                    paths.add(path)
                    members.append(entry)
                content.extractall(destination, members=members, filter='data')


def run(*args, cwd=None, data=None, timeout=60):
    subprocess.run(args, input=data, cwd=cwd, check=True, timeout=timeout,
                   env={'PATH': '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': '/root', 'LANG': 'C.UTF-8'})


def native_build_commands(stage, revision, native_revision):
    """The native server builds with cc from the artifact's generated C, when present."""
    if not (stage / 'build/native-release/server').is_dir():
        return []
    return [
        ('/usr/bin/make', '-j2', 'build-native', f'VERSION={revision}', f'SOURCE_COMMIT={revision}',
         f'LIBOQS_COMMIT={native_revision}'),
        ('/usr/bin/python3', '-B', 'scripts/verify_deployment_inputs.py', 'verify-artifact-manifest',
         '--output', 'mesh-native', '--manifest', 'build/mesh-native.manifest.json',
         '--expected-source-commit', revision, '--expected-liboqs-commit', native_revision),
    ]


def check_revision(revision, attempts=15):
    for attempt in range(attempts):
        try:
            with open_url('http://127.0.0.1:8080/readyz', timeout=4) as local, open_url(PUBLIC + '/readyz', timeout=4) as public:
                if local.status == public.status == 200:
                    with open_url(PUBLIC + '/api/v1/node', timeout=4) as node:
                        if json.loads(node.read(65536)).get('build_version') == revision:
                            return True
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(2)
    return False


def deploy(run_id, attempt, revision, native_revision, native_digest, native_entries, artifact_id, token):
    def get(path):
        return get_api(path, token)

    verify_run(run_id, attempt, revision, artifact_id, get)
    if check_revision(revision, attempts=1):
        print(f'Mesh {revision} is already ready locally and on {PUBLIC}', flush=True)
        return
    with tempfile.TemporaryDirectory(prefix='.mesh-gitea-', dir=SOURCE.parent) as temporary:
        stage = Path(temporary) / 'source'
        stage.mkdir(mode=0o700)
        with tempfile.TemporaryFile(dir=temporary) as bundle:
            with open_url(f'{API}/actions/artifacts/{artifact_id}/zip', token, timeout=30) as download:
                copy_limited(download, bundle, MAX_ARCHIVE)
            bundle.seek(0)
            unpack_source(bundle, stage)
        run('/usr/bin/python3', '-B', 'scripts/verify_deployment_inputs.py', 'verify-source',
            '--source-dir', 'vendor/liboqs', '--expected-commit', native_revision,
            '--expected-digest', native_digest, '--expected-entries', native_entries, cwd=stage)
        run('/usr/bin/make', 'build', f'VERSION={revision}', f'SOURCE_COMMIT={revision}',
            f'LIBOQS_COMMIT={native_revision}', cwd=stage, timeout=900)
        run('/usr/bin/python3', '-B', 'scripts/verify_deployment_inputs.py', 'verify-artifact-manifest',
            '--output', 'mesh', '--manifest', 'build/mesh.manifest.json', '--expected-source-commit', revision,
            '--expected-liboqs-commit', native_revision, cwd=stage)
        for command in native_build_commands(stage, revision, native_revision):
            run(*command, cwd=stage, timeout=900)
        require_master(revision, get)
        old = Path(temporary) / 'previous-source'
        if not SOURCE.is_dir() or SOURCE.is_symlink():
            raise DeployError('Expected the existing Mesh source directory')
        SOURCE.rename(old)
        try:
            stage.rename(SOURCE)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            install = json.dumps({'stamp': stamp, 'source_commit': revision,
                                  'native_commit': native_revision, 'mesh_token': ''}).encode()
            run('/usr/bin/python3', str(SOURCE / 'deploy/install.py'), data=install, timeout=180)
            if not check_revision(revision):
                run('/bin/sh', str(SOURCE / 'deploy/rollback.sh'), data=stamp.encode(), timeout=120)
                raise DeployError('Mesh readiness or revision failed and the release was rolled back')
        except BaseException:
            if SOURCE.exists():
                shutil.rmtree(SOURCE)
            old.rename(SOURCE)
            raise
    print(f'Mesh {revision} is ready locally and on {PUBLIC}', flush=True)


def main():
    command = COMMAND.fullmatch(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    if command is None:
        raise DeployError('Only the verified Mesh deployment operation is allowed')
    raw_token = sys.stdin.buffer.read(4098).rstrip(b'\n')
    if TOKEN.fullmatch(raw_token) is None:
        raise DeployError('Expected a short-lived Gitea job token')
    run_id, attempt, revision, native_revision, native_digest, native_entries, artifact_id = command.groups()
    os.umask(0o077)
    with open('/run/lock/mesh-gitea-deploy.lock', 'ab') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deploy(int(run_id), int(attempt), revision, native_revision, native_digest, native_entries,
               int(artifact_id), raw_token.decode('ascii'))


if __name__ == '__main__':
    try:
        main()
    except DeployError as error:
        sys.exit(str(error))
    except urllib.error.HTTPError as error:
        sys.exit(f'Mesh deployment HTTP request returned {error.code}')
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        sys.exit(f'Mesh deployment failed ({type(error).__name__})')
