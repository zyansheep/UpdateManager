"""Packages in the user's nix profile (`nix profile list`): which have a newer
version in the flake they came from, how old they are, and what upgrading costs.

Also releases the profile's flakes don't know about yet: the Z3 Code desktop app
(t3code-vendored) is packaged by hand from the fork's releases (tags vX.Y.Z-z3.N).
"""
import fnmatch
import json
import re
import time
import urllib.parse

from . import config
from .system import parse_dry_run
from .util import CommandError, run, store_name

VERSION_TAG = re.compile(r'^v?(\d+(?:\.\d+)*)(.*)$')


def profile_list(timeout=30):
    """{name: element} from `nix profile list --json` (Nix 2.20+ names elements)."""
    data = json.loads(run(['nix', 'profile', 'list', '--json'], timeout=timeout))
    elements = data.get('elements', {})
    if isinstance(elements, list):  # Nix before 2.20: a list, no names
        elements = {store_name((e.get('storePaths') or ['?'])[0]): e for e in elements}
    return elements


def locked_params(url):
    """path:/x?lastModified=1&narHash=...&rev=... -> {'lastModified': 1, 'narHash', 'rev'}."""
    parsed = urllib.parse.urlsplit(url or '')
    params = dict(urllib.parse.parse_qsl(parsed.query))
    out = {k: params[k] for k in ('narHash', 'rev') if k in params}
    if parsed.scheme == 'github' and not out.get('rev'):
        parts = parsed.path.split('/')
        if len(parts) >= 3 and re.fullmatch(r'[0-9a-f]{40}', parts[2]):
            out['rev'] = parts[2]
    if 'lastModified' in params:
        try:
            out['lastModified'] = int(params['lastModified'])
        except ValueError:
            pass
    return out


def latest_metadata(original_url, timeout=120):
    """What original_url resolves to now: {'rev', 'narHash', 'lastModified'}."""
    data = json.loads(run(['nix', 'flake', 'metadata', '--json', '--refresh', original_url], timeout=timeout))
    locked = data.get('locked', {})
    return {'rev': locked.get('rev') or data.get('revision'), 'narHash': locked.get('narHash'),
            'lastModified': locked.get('lastModified') or data.get('lastModified')}


def is_newer(locked, latest):
    """True if latest differs from what the profile locked (by rev, else by content)."""
    if latest.get('rev') and locked.get('rev'):
        return latest['rev'] != locked['rev']
    if latest.get('narHash') and locked.get('narHash'):
        return latest['narHash'] != locked['narHash']
    return False


def check(previous=None, now=None, estimate=True, timeout=120):
    """The profile section: [{'name', 'version', 'source', 'locked_age_days', 'update', 'latest_age_days',
    'estimate', 'release'}], checking each distinct flake once."""
    now = now or time.time()
    items, errors = [], {}
    try:
        elements = profile_list()
    except (CommandError, ValueError) as e:
        return {'checked': now, 'items': [], 'error': str(e)}
    seen = {}
    for name, element in sorted(elements.items()):
        original = element.get('originalUrl') or ''
        locked = locked_params(element.get('url'))
        path = (element.get('storePaths') or [''])[0]
        item = {'name': name, 'installed': store_name(path), 'source': original,
                'attr': element.get('attrPath'), 'locked_modified': locked.get('lastModified')}
        if locked.get('lastModified'):
            item['locked_age_days'] = round((now - locked['lastModified']) / 86400, 1)
        if original and original not in seen:
            try:
                seen[original] = latest_metadata(original, timeout=timeout)
            except CommandError as e:
                last = (e.stderr or '').strip().splitlines()[-1:] or [str(e)]
                seen[original] = {'error': last[0].removeprefix('error: ')[:200]}
            except ValueError as e:
                seen[original] = {'error': str(e)[:200]}
        latest = seen.get(original, {})
        if latest.get('error'):
            item['error'] = latest['error']
        else:
            item['update'] = is_newer(locked, latest)
            if latest.get('lastModified'):
                item['latest_modified'] = latest['lastModified']
        items.append(item)
    if estimate:
        old = {i['name']: i for i in (previous or {}).get('items', [])}
        for item in items:
            if not item.get('update'):
                continue
            was = old.get(item['name'], {})
            if was.get('estimate') and was.get('latest_modified') == item.get('latest_modified'):
                item['estimate'] = was['estimate']
                continue
            try:
                item['estimate'] = upgrade_estimate(item['source'], item['attr'])
            except CommandError as e:
                item['estimate'] = {'error': str(e)[:200]}
    releases = release_checks(items)
    out = {'checked': now, 'items': items, 'releases': releases}
    if errors:
        out['errors'] = errors
    return out


def upgrade_estimate(source, attr, timeout=300):
    """What `nix profile upgrade` would fetch and build (nix build --dry-run of the flake's attr)."""
    done = run(['nix', 'build', '--dry-run', '--no-link', f'{source}#{attr}'], timeout=timeout, check=False)
    return parse_dry_run(done.stderr, done.returncode)


def version_key(tag):
    m = VERSION_TAG.match(tag)
    if not m:
        return ((), tag)
    nums = tuple(int(x) for x in m[1].split('.'))
    suffix = tuple(int(x) if x.isdigit() else x for x in re.split(r'(\d+)', m[2]) if x)
    return (nums, suffix)


def newest_tag(text, pattern):
    tags = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith('refs/tags/') and not parts[1].endswith('^{}'):
            tag = parts[1][len('refs/tags/'):]
            if fnmatch.fnmatch(tag, pattern):
                tags.append(tag)
    return max(tags, key=version_key) if tags else None


def release_checks(items, settings=None, timeout=20):
    """Configured release feeds for profile packages: the newest tag matching the pattern."""
    out = []
    names = {i['name']: i for i in items}
    for name, feed in (config.get('releases', settings=settings) or {}).items():
        if name not in names:
            continue
        entry = {'name': name, 'repo': feed['repo'], 'label': feed.get('label') or name,
                 'installed': names[name].get('installed')}
        try:
            text = run(['git', 'ls-remote', '--tags', feed['repo']], timeout=timeout)
            entry['latest'] = newest_tag(text, feed.get('tags', '*'))
        except CommandError as e:
            entry['error'] = str(e)[:200]
        out.append(entry)
    return out
