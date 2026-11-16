"""Write each app's release history; every archived release and blob stays.

The history lists the newest release of every compatibility group, then the
newest others up to ten. A group is what a host runs: one payload format
(.zx, legacy .zib, or a release's WebAssembly variant), one ZX revision and
one host and module interface. So hosts released before a new format,
revision or interface keep their last release while newer hosts move on.

Called under the store publication lock. Unknown files and malformed metadata
are left alone. No live store is modified by importing this.
"""
import json
from pathlib import Path
import re

HASH = re.compile(r'[0-9a-f]{64}\Z')
IDENTIFIER = re.compile(r'[a-z0-9][a-z0-9_.-]*\Z')
RETAINED_RELEASES = 10
# The node reads at most this many history entries.
HISTORY_LIMIT = 64


def read_release(path, app):
    try:
        if path.is_symlink():
            return None
        if path.stat().st_size > 65536:
            return None
        value = json.loads(path.read_bytes())
        if (value.get('app_id') != app or type(value.get('sequence')) is not int
                or value['sequence'] < 1 or not isinstance(value.get('artifacts'), list)):
            return None
        if not value['artifacts'] or any(not isinstance(a, dict) or
                not isinstance(a.get('sha256'), str) or not HASH.fullmatch(a['sha256'])
                for a in value['artifacts']):
            return None
        dependencies = value.get('dependencies', [])
        if not isinstance(dependencies, list) or any(not isinstance(d, dict) or
                not isinstance(d.get('app_id'), str) or not IDENTIFIER.fullmatch(d['app_id']) or
                not isinstance(d.get('sha256'), str) or not HASH.fullmatch(d['sha256'])
                for d in dependencies):
            return None
        return value
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def artifact_format(root, value):
    """.zx or legacy .zib: the suffix the release's first artifact is stored with."""
    digest = value['artifacts'][0]['sha256']
    if (root / (digest + '.zib')).is_file() and not (root / (digest + '.zx')).is_file():
        return '.zib'
    return '.zx'


def zx_revision(path):
    """The executable revision in a stored .zx header, or 0."""
    try:
        with path.open('rb') as source:
            header = source.read(8)
    except OSError:
        return 0
    if len(header) != 8 or not header.startswith(b'ZX\0\0'):
        return 0
    return int.from_bytes(header[4:], 'little')


def has_wasm_variant(root, value):
    """Whether the release's WebAssembly variant and its module are stored."""
    variant = read_release(root / f"release-{value['sequence']}.wasm.json", root.name)
    return variant is not None and variant['sequence'] == value['sequence'] and \
        (root / (variant['artifacts'][0]['sha256'] + '.wasm')).is_file()


def compatibility_groups(root, value):
    """The groups of hosts that run this release."""
    found = artifact_format(root, value)
    revision = zx_revision(root / (value['artifacts'][0]['sha256'] + '.zx')) if found == '.zx' else 0
    interfaces = (revision, value.get('host_api'), value.get('module_api'))
    groups = [(found, *interfaces)]
    if found == '.zx' and has_wasm_variant(root, value):
        groups.append(('.wasm', *interfaces))
    return groups


def retained_choices(values):
    """The newest release of every compatibility group, then the newest others."""
    kept = []
    groups = set()
    for item in values:
        found = set(compatibility_groups(item[0].parent, item[1])) - groups
        if found and len(kept) < HISTORY_LIMIT:
            groups.update(found)
            kept.append(item)
    for item in values:
        if len(kept) >= RETAINED_RELEASES:
            break
        if item not in kept:
            kept.append(item)
    kept.sort(key=lambda item: item[1]['sequence'], reverse=True)
    return kept


def retain(store, write):
    store = Path(store)
    for root in store.iterdir():
        if not root.is_dir() or root.is_symlink() or not IDENTIFIER.fullmatch(root.name):
            continue
        values = []
        for path in root.glob('release-*.json'):
            value = read_release(path, root.name)
            if value is not None and path.name == f"release-{value['sequence']}.json":
                values.append((path, value))
        values.sort(key=lambda item: item[1]['sequence'], reverse=True)
        history = [value for _, value in retained_choices(values)]
        latest = read_release(root / 'latest-v2.json', root.name)
        if not history and latest is not None:
            history = [latest]
        if history:
            write(root / 'history-v2.json', (json.dumps({'versions': history}, separators=(',', ':')) + '\n').encode())
