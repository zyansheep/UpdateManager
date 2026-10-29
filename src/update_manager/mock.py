"""A stand-in for a machine that can't answer (for testing on functor while the laptop sleeps).

`update-manager agent --mock-peer isomorph` makes up isomorph's own state the way
isomorph's agent would, from what functor knows: its current system is the
previous nixos-ci build for it, so its update section (closure diff, download
estimate) is computed for real from those two systems in functor's store.
The profile list and checkout are invented, and the card says "mock".
"""
import os
import time
from pathlib import Path

from . import ci, system
from .util import store_name


def host_state(name, ci_state, now=None, settings=None):
    now = now or time.time()
    items = (ci_state or {}).get('roots', {}).get(name) or []
    target = ci.target_for(name, ci_state)
    older = [i for i in items if not target or i['path'] != target['path']]
    current = (older[-1] if older else (items[0] if items else None))  # the oldest kept build: a real diff
    path = current['path'] if current else None
    sysinfo = {'current': path, 'booted': path, 'generation': 412, 'switched': (current or {}).get('stamp'),
               'rev': (current or {}).get('rev12'), 'rev_source': 'nixos-ci'}
    if path and Path(path).exists():
        try:
            sysinfo['version'] = (Path(path) / 'nixos-version').read_text().strip()
        except OSError:
            sysinfo['version'] = None
        sysinfo['kernel'] = system.kernel_version(os.path.realpath(Path(path) / 'kernel'))
        sysinfo['booted_kernel'] = sysinfo['kernel']
    sysinfo['reboot_pending'] = False
    out = {'host': name, 'collected': now - 40, 'label': 'Framework laptop', 'mock': True,
           'capabilities': {'switch_units': False, 'rebuild': True, 'nixos_ci': False, 'forks': False},
           'system': sysinfo, 'errors': {}}
    if ci_state:
        out['update'] = system.update_section(name, sysinfo, ci_state, settings, None, have_local=True)
    out['checkout'] = {'path': '~/nixos-conf', 'branch': 'main', 'head': (current or {}).get('rev12'),
                       'upstream': 'origin/main', 'ahead': 0, 'behind': len((out.get('update') or {})
                                                                           .get('commits') or []),
                       'dirty': 0, 'fetched': now - 3600}
    out['profiles'] = {'checked': now - 1800, 'items': [
        {'name': 't3code-vendored', 'installed': 't3code-0.1.0+vendored.11',
         'source': 'path:/home/zyansheep/Repos/t3code-nix', 'attr': 'packages.x86_64-linux.t3code-vendored',
         'update': False, 'locked_age_days': 2.1},
        {'name': 't3', 'installed': 't3-cli-0.1.0', 'source': 'path:/home/zyansheep/Repos/t3code-nix',
         'attr': 'packages.x86_64-linux.t3', 'update': True, 'locked_age_days': 13.6,
         'estimate': {'paths': 3, 'download': 41.2 * 2 ** 20, 'unpacked': 188 * 2 ** 20, 'builds': 1, 'ok': True}},
    ], 'releases': [{'name': 't3code-vendored', 'label': 'Z3 Code release', 'repo': 'https://github.com/zyansheep/t3code',
                     'installed': 't3code-0.1.0+vendored.11', 'latest': None}]}
    out['jobs'] = [{'id': 'mock-1', 'kind': 'rebuild', 'title': 'Rebuild from the checkout', 'state': 'done',
                    'created': now - 2 * 86400, 'started': now - 2 * 86400, 'finished': now - 2 * 86400 + 214,
                    'message': f"rebuilt and switched to {store_name(path)}"}]
    return out


def own_state(name, ci_state, now=None, settings=None):
    now = now or time.time()
    return {'version': 1, 'host': name, 'time': now - 40, 'self': host_state(name, ci_state, now, settings)}
