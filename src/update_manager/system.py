"""This machine's NixOS system: what runs, what was booted, what the CI built for it,
what changes between them (a closure diff like `nix store diff-closures`), what a
switch would download and how long it would take, and the state of the checkout.
"""
import json
import os
import re
import statistics
import time
from pathlib import Path

from . import ci, config
from .util import CommandError, expand, read_json, run, store_name

DRV_NAME = re.compile(r'^(.*?)-(\d.*)$')  # Nix's DrvName: the version starts at the first "-<digit>".
FETCH = re.compile(r'these (\d+) paths? will be fetched \(([\d.]+) (\w+) download, ([\d.]+) (\w+) unpacked\)')
BUILD = re.compile(r'these (\d+) derivations? will be built')
UNITS = {'B': 1, 'KiB': 1024, 'MiB': 1024 ** 2, 'GiB': 1024 ** 3, 'TiB': 1024 ** 4}
KERNEL = re.compile(r'^linux-(\d[\w.\-]*)$')
# Packages whose change matters more than its size: listed first in the diff.
NOTABLE = ('linux', 'systemd', 'nix', 'glibc', 'mesa', 'niri', 'waybar', 'firefox', 'floorp', 'gtk4', 'pipewire',
           'nvidia-x11', 'amdgpu', 'linux-firmware', 'zfs-kernel', 'zfs-user', 't3code', 'drainhound')


def realpath(path):
    try:
        return os.path.realpath(path)
    except OSError:
        return None


def local_system(root='/'):
    """{'current', 'booted', 'generation', 'switched', 'version', 'kernel', 'booted_kernel', 'reboot_pending'}."""
    root = Path(root)
    current = realpath(root / 'run/current-system')
    booted = realpath(root / 'run/booted-system')
    out = {'current': current, 'booted': booted}
    profile = root / 'nix/var/nix/profiles/system'
    try:
        link = os.readlink(profile)
        m = re.match(r'system-(\d+)-link', link)
        out['generation'] = int(m[1]) if m else None
        out['switched'] = os.lstat(profile.parent / link).st_mtime
    except OSError:
        out['generation'] = out['switched'] = None
    try:
        out['version'] = (Path(current) / 'nixos-version').read_text().strip()
    except (OSError, TypeError):
        out['version'] = None
    out['kernel'] = kernel_version(realpath(Path(current or '/nonexistent') / 'kernel'))
    out['booted_kernel'] = kernel_version(realpath(Path(booted or '/nonexistent') / 'kernel'))
    out['reboot_pending'] = bool(out['kernel'] and out['booted_kernel'] and out['kernel'] != out['booted_kernel'])
    return out


def kernel_version(path):
    """/nix/store/...-linux-6.18.55/bzImage -> 6.18.55."""
    if not path:
        return None
    name = store_name(os.path.dirname(path) if path.endswith(('bzImage', 'Image', 'vmlinuz')) else path)
    m = KERNEL.match(name)
    return m[1] if m else None


def configuration_revision(system_path):
    """system.configurationRevision, if the flake sets it (nixos-version --configuration-revision)."""
    exe = Path(system_path or '/nonexistent') / 'sw/bin/nixos-version'
    if not exe.exists():
        return None
    try:
        rev = run([exe, '--configuration-revision'], timeout=5).strip()
    except CommandError:
        return None
    return rev if re.fullmatch(r'[0-9a-f]{7,40}(-dirty)?', rev) else None


# --- closures --------------------------------------------------------------------

def path_infos(path, store=None, timeout=120):
    """{path: {'narSize', 'downloadSize'?, 'references'}} for path's closure, from the local store
    or a binary cache (store=URL)."""
    args = ['nix', 'path-info', '--recursive', '--json']
    if store:
        args += ['--store', store]
    text = run([*args, path], timeout=timeout)
    data = json.loads(text)
    if isinstance(data, list):  # Nix before 2.19 printed a list of objects with a 'path' key
        data = {item['path']: item for item in data if item.get('path')}
    return {p: info for p, info in data.items() if info}


def split_name(name):
    m = DRV_NAME.match(name)
    return (m[1], m[2]) if m else (name, '')


def closure_groups(infos):
    """{pname: {'versions': set, 'size': bytes}}: what diff-closures compares."""
    groups = {}
    for path, info in infos.items():
        pname, version = split_name(store_name(path))
        g = groups.setdefault(pname, {'versions': set(), 'size': 0})
        if version:
            g['versions'].add(version)
        g['size'] += info.get('narSize') or 0
    return groups


def diff_closures(old, new, threshold=8 * 1024):
    """Like `nix store diff-closures`: packages whose versions changed, or whose size changed by
    more than threshold. Notable packages (the kernel, systemd, ...) first, then by size change."""
    a, b = closure_groups(old), closure_groups(new)
    changes = []
    for name in sorted(set(a) | set(b)):
        ga, gb = a.get(name), b.get(name)
        va = sorted(ga['versions']) if ga else None
        vb = sorted(gb['versions']) if gb else None
        delta = (gb['size'] if gb else 0) - (ga['size'] if ga else 0)
        if va != vb or abs(delta) >= threshold:
            changes.append({'name': name, 'from': va, 'to': vb, 'delta': delta})

    def key(c):
        changed = c['from'] != c['to'] and c['from'] is not None and c['to'] is not None
        notable = c['name'] in NOTABLE and changed
        return (not notable, not changed, -abs(c['delta']))
    changes.sort(key=key)
    total = sum(i.get('narSize') or 0 for i in new.values()) - sum(i.get('narSize') or 0 for i in old.values())
    versions = [c for c in changes if c['from'] is not None and c['to'] is not None and c['from'] != c['to']]
    return {
        'changes': changes[:60], 'count': len(changes),
        'upgraded': len(versions),
        'added': sum(1 for c in changes if c['from'] is None),
        'removed': sum(1 for c in changes if c['to'] is None),
        'size_delta': total,
        'kernel': kernel_change(old, new),
    }


def kernel_change(old, new):
    def kernels(infos):
        return {m[1] for p in infos if (m := KERNEL.match(store_name(p)))}
    ka, kb = kernels(old), kernels(new)
    if ka != kb and kb:
        return {'from': ', '.join(sorted(ka)) or None, 'to': ', '.join(sorted(kb))}
    return None


def version_text(versions):
    if versions is None:
        return '∅'
    return ', '.join(versions) if versions else 'ε'


# --- estimates -------------------------------------------------------------------

def dry_run(target, substituters=(), timeout=300):
    """What realising target here would fetch and build: {'paths', 'download', 'unpacked', 'builds'}.
    Nix's own numbers: it asks every substituter in priority order, so this is the real download."""
    args = ['nix-store', '--realise', '--dry-run', target]
    for s in substituters:
        args += ['--option', 'extra-substituters', s]
    done = run(args, timeout=timeout, check=False)
    return parse_dry_run(done.stderr + done.stdout, done.returncode)


def parse_dry_run(text, code=0):
    out = {'paths': 0, 'download': 0, 'unpacked': 0, 'builds': 0, 'ok': code == 0}
    if m := FETCH.search(text):
        out['paths'] = int(m[1])
        out['download'] = float(m[2]) * UNITS.get(m[3], 1)
        out['unpacked'] = float(m[4]) * UNITS.get(m[5], 1)
    if m := BUILD.search(text):
        out['builds'] = int(m[1])
    if code != 0:
        out['error'] = (text.strip().splitlines() or ['nix-store failed'])[-1][:300]
    return out


def missing_estimate(target_infos, have):
    """Download for target_infos when the store already holds `have` (a set of paths): an upper
    bound (functor's cache serves uncompressed NARs; cache.nixos.org's xz is ~3x smaller)."""
    missing = {p: i for p, i in target_infos.items() if p not in have}
    return {'paths': len(missing),
            'download': sum(i.get('downloadSize') or i.get('narSize') or 0 for i in missing.values()),
            'unpacked': sum(i.get('narSize') or 0 for i in missing.values()),
            'builds': 0, 'ok': True, 'approximate': True}


def switch_history():
    """The root switch units' record of past switches: [{'bytes', 'download_s', 'activation_s', ...}]."""
    data = read_json(config.SYSTEM_STATE / 'history.json', [])
    return data if isinstance(data, list) else []


def time_estimate(download, settings=None, history=None):
    """Seconds for a switch that downloads `download` bytes: at the median measured rate (else the
    configured guess), plus the median activation time."""
    est = config.get('estimates', settings=settings)
    history = switch_history() if history is None else history
    rates = [h['bytes'] / h['download_s'] for h in history[-10:]
             if h.get('bytes', 0) > 20e6 and h.get('download_s', 0) > 1]
    acts = [h['activation_s'] for h in history[-10:] if h.get('activation_s')]
    rate = statistics.median(rates) if rates else est['download_bps']
    activation = statistics.median(acts) if acts else est['activation_s']
    return {'seconds': round((download or 0) / rate + activation), 'rate': rate, 'activation': activation,
            'measured': bool(rates)}


# --- the update section ----------------------------------------------------------

def update_section(host, system, ci_state, settings=None, previous=None, have_local=True, now=None, heavy=True):
    """What switching this host to its newest CI build means. `system` is local_system()'s
    result (or a peer's); have_local says whether target paths can be inspected in this store.
    heavy=False leaves out the diff and the download estimate unless previous has them for these
    two systems (then 'pending' says the slow loop still has to work them out)."""
    now = now or time.time()
    target = ci.target_for(host, ci_state)
    current = system.get('current')
    out = {'computed': now, 'target': target}
    if not target:
        out['state'] = 'no-ci'
        return out
    out['up_to_date'] = current == target['path']
    rev = system.get('rev') or ci.rev_of(current, ci_state)
    out['current_rev'] = rev
    out['target_rev'] = (target.get('rev') or '')[:12] or None
    out['commits'] = commits_between(rev, target.get('rev'), settings)
    out['behind'] = len(out['commits']) if out['commits'] is not None else None
    if out['up_to_date']:
        out['state'] = 'current'
        return out
    # Not every other system is older than CI's: a machine may run a local build (rebuilt from a
    # checkout with changes CI hasn't seen) or a newer commit. Only an older one is an update.
    target_rev = (target.get('rev') or '')[:12]
    if not rev or rev.endswith('-dirty'):
        out['state'] = 'local'
    elif out['commits'] == [] and rev[:12] != target_rev:
        out['state'] = 'ahead'
    else:
        out['state'] = 'available'
    # The diff and download estimate only change when one of the two systems does.
    key = f"{current}->{target['path']}"
    if previous and previous.get('key') == key and previous.get('diff') is not None:
        for k in ('diff', 'download', 'estimate', 'estimated_on'):
            if k in previous:
                out[k] = previous[k]
        out['key'] = key
        return out
    out['key'] = key
    if not heavy:
        out['pending'] = True
        return out
    try:
        target_infos = None
        if have_local and Path(target['path']).exists():
            target_infos = path_infos(target['path'])
        else:
            target_infos = path_infos(target['path'], store=config.get('cache_url', settings=settings))
        old_infos = path_infos(current) if current and Path(current).exists() else {}
        out['diff'] = diff_closures(old_infos, target_infos)
        if have_local and host == config.hostname():
            if Path(target['path']).exists():
                out['download'] = {'paths': 0, 'download': 0, 'unpacked': 0, 'builds': 0, 'ok': True}
            else:
                out['download'] = dry_run(target['path'], [config.get('cache_url', settings=settings)])
        else:
            # On behalf of another machine: it has at least its current closure.
            out['download'] = missing_estimate(target_infos, set(old_infos))
            out['estimated_on'] = config.hostname()
        out['estimate'] = time_estimate(out['download'].get('download'), settings)
    except (CommandError, ValueError, OSError) as e:
        out['error'] = str(e)
    return out


def commits_between(old, new, settings=None, limit=30):
    """[{'rev', 'time', 'subject'}] in new but not old, newest first, from the flake checkout
    (or nixos-ci's own clone). None when old is unknown."""
    if not old or not new:
        return None
    if old[:12] == new[:12]:
        return []
    for repo, extra in ((config.flake_dir(settings), []),
                        (Path(config.get('nixos_ci', 'state', settings=settings)) / 'checkout-main',
                         ['-c', 'safe.directory=*'])):
        if not (Path(repo) / '.git').exists():
            continue
        try:
            text = run(['git', *extra, '-C', repo, 'log', f'--max-count={limit}', '--format=%H%x09%ct%x09%s',
                        f'{old}..{new}'], timeout=15)
        except CommandError:
            continue
        out = []
        for line in text.splitlines():
            rev, when, subject = (line.split('\t', 2) + ['', ''])[:3]
            out.append({'rev': rev[:12], 'time': float(when or 0), 'subject': subject})
        return out
    return None


def checkout(settings=None):
    """The nixos-conf checkout: branch, HEAD, ahead/behind its upstream, uncommitted files."""
    repo = config.flake_dir(settings)
    if not (repo / '.git').exists():
        return None
    out = {'path': str(repo).replace(str(Path.home()), '~', 1)}
    try:
        text = run(['git', '--no-optional-locks', '-C', repo, 'status', '--porcelain=v2', '--branch'], timeout=15)
    except CommandError as e:
        return dict(out, error=str(e))
    dirty = 0
    for line in text.splitlines():
        if line.startswith('# branch.oid '):
            out['head'] = line.split()[2][:12]
        elif line.startswith('# branch.head '):
            out['branch'] = line.split()[2]
        elif line.startswith('# branch.upstream '):
            out['upstream'] = line.split()[2]
        elif line.startswith('# branch.ab '):
            ahead, behind = line.split()[2:4]
            out['ahead'], out['behind'] = int(ahead), -int(behind)
        elif line and not line.startswith('#') and not line.startswith('? '):
            dirty += 1
    out['dirty'] = dirty
    try:
        out['fetched'] = (repo / '.git/FETCH_HEAD').stat().st_mtime
    except OSError:
        out['fetched'] = None
    return out


def fetch_checkout(settings=None):
    """git fetch the checkout's remote (only remote-tracking refs move)."""
    repo = config.flake_dir(settings)
    remote = config.get('flake_remote', settings=settings)
    run(['git', '-C', repo, 'fetch', '--quiet', remote], timeout=60)


# --- flake inputs ------------------------------------------------------------------

def flake_inputs(lock_path, now=None):
    """The root inputs of flake.lock: [{'name', 'type', 'repo', 'ref', 'rev', 'modified', 'age_days'}]."""
    now = now or time.time()
    try:
        lock = json.loads(Path(lock_path).read_text())
    except (OSError, ValueError):
        return []
    nodes = lock.get('nodes', {})
    root = nodes.get(lock.get('root', 'root'), {})
    out = []
    for name, ref in sorted(root.get('inputs', {}).items()):
        if isinstance(ref, list):  # a follows: not an input of its own
            continue
        node = nodes.get(ref, {})
        locked, original = node.get('locked', {}), node.get('original', {})
        if not locked:
            continue
        repo = f"{locked.get('owner', '')}/{locked.get('repo', '')}" if locked.get('owner') else locked.get('url')
        modified = locked.get('lastModified')
        out.append({'name': name, 'type': locked.get('type'), 'repo': repo, 'ref': original.get('ref'),
                    'pinned': bool(original.get('rev')), 'rev': (locked.get('rev') or '')[:12],
                    'modified': modified, 'age_days': round((now - modified) / 86400, 1) if modified else None})
    return out


def input_freshness(inputs, timeout=20):
    """Ask each github input's remote for its branch head (git ls-remote): 'latest' and 'stale'."""
    for item in inputs:
        if item.get('type') != 'github' or item.get('pinned') or not item.get('repo'):
            continue
        ref = item.get('ref') or 'HEAD'
        refs = [ref] if ref == 'HEAD' else [f'refs/heads/{ref}', f'refs/tags/{ref}']
        try:
            text = run(['git', 'ls-remote', f"https://github.com/{item['repo']}", *refs], timeout=timeout)
        except CommandError as e:
            item['check_error'] = str(e)[:120]
            continue
        heads = [line.split()[0] for line in text.splitlines() if line.strip()]
        if heads:
            item['latest'] = heads[0][:12]
            item['stale'] = not heads[0].startswith(item.get('rev') or '-')
    return inputs


def lock_path_for(settings=None):
    return expand(config.get('flake', settings=settings)) / 'flake.lock'
