"""Paths and settings: /etc/update-manager/config.toml, then ~/.config/update-manager/config.toml.

Every key is optional; DEFAULTS describes the two machines this grew on
(functor builds and caches, isomorph is the laptop). The NixOS module writes
/etc/update-manager/config.toml from `services.update-manager.settings`.
"""
import copy
import os
import socket
import sys
import tomllib
from pathlib import Path

from .util import expand

APP_ID = 'org.zyansheep.UpdateManager'
APP_PATH = '/org/zyansheep/UpdateManager'

HOME = Path.home()
RUNTIME = Path(os.environ.get('UPDATE_MANAGER_RUNTIME_DIR')
               or Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'update-manager')
CACHE = Path(os.environ.get('UPDATE_MANAGER_CACHE_DIR')
             or Path(os.environ.get('XDG_CACHE_HOME', HOME / '.cache')) / 'update-manager')
STATE = Path(os.environ.get('UPDATE_MANAGER_STATE_DIR')
             or Path(os.environ.get('XDG_STATE_HOME', HOME / '.local/state')) / 'update-manager')
CONFIG_FILES = [Path('/etc/update-manager/config.toml'),
                Path(os.environ.get('XDG_CONFIG_HOME', HOME / '.config')) / 'update-manager/config.toml']
# Written by the root switch units (helper/update-manager-switch); world-readable.
SYSTEM_RUNTIME = Path(os.environ.get('UPDATE_MANAGER_SYSTEM_RUNTIME', '/run/update-manager'))
SYSTEM_STATE = Path(os.environ.get('UPDATE_MANAGER_SYSTEM_STATE', '/var/lib/update-manager'))

DEFAULTS = {
    # The machine that runs nixos-ci and serves the binary cache.
    'ci_host': 'functor',
    'cache_url': 'http://100.64.0.4:5000',
    # The nixos-conf checkout `rebuild` uses, for commit subjects and local changes.
    'flake': '~/nixos-conf',
    'flake_remote': 'origin',
    'flake_branch': 'main',
    'nixos_ci': {'state': '/var/lib/nixos-ci', 'units': ['nixos-ci.service', 'nixos-ci-poll.service',
                                                          'nixos-ci-update.service'],
                 'timer': 'nixos-ci-poll.timer'},
    # Machines. `ssh` is where to reach one; `pull = false` for a machine that
    # takes no ssh connections (isomorph has no sshd): it pushes its state to
    # the CI host each time it pulls, and picks up work queued for it there.
    'hosts': {
        'functor': {'ssh': 'functor', 'pull': True, 'label': 'Desktop · CI · cache'},
        'isomorph': {'ssh': 'isomorph', 'pull': False, 'label': 'Framework laptop'},
    },
    'remote': {
        # What runs on the other machine. A list of candidates: the first that exists wins.
        'command': ['/run/current-system/sw/bin/update-manager', '~/.nix-profile/bin/update-manager'],
        'ssh_options': ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=10',
                        '-o', 'ServerAliveCountMax=2'],
    },
    'intervals': {'fast': 15, 'busy': 3, 'peer': 30, 'peer_busy': 8, 'slow': 900,
                  'profiles': 6 * 3600, 'inputs': 6 * 3600, 'forks': 24 * 3600},
    # Throughput guesses until a switch has measured one (bytes/s), and activation time (s).
    'estimates': {'download_bps': 25e6, 'activation_s': 40},
    # Nix profile packages: a newer release than the profile's flake knows about.
    'releases': {
        't3code-vendored': {'repo': 'https://github.com/zyansheep/t3code', 'tags': 'v*-z3.*',
                            'label': 'Z3 Code release'},
    },
    # Forks and vendored code to check against their upstreams (on the machine that has the repos).
    # japanese-weave is left out on purpose: it is an independent fork of french-weave now.
    'forks_host': 'functor',
    'forks': [
        {'name': 'Z3 Code', 'repo': '~/Repos/t3code', 'branch': 'z3', 'fallback_branches': ['vendored'],
         'upstream': 'upstream', 'track': 'tag', 'tags': 'v[0-9]*.[0-9]*.[0-9]*', 'exclude': '-',
         'strategy': 'rebase', 'push': 'origin',
         'notes': "The user's T3 Code fork: the latest pingdotgg/t3code stable tag plus their patches. "
                  "CI publishes releases named vX.Y.Z-z3.N from it."},
        {'name': 't3code-nix', 'repo': '~/Repos/t3code-nix', 'branch': 'track-upstream-nightly',
         'upstream': 'origin', 'track': 'branch', 'upstream_branch': 'main', 'strategy': 'merge',
         'push': None,
         'notes': "Packaging for T3 Code. origin is Sawrz/t3code-nix (upstream): never push to it. "
                  "The user's branch adds the vendored Z3 Code package."},
        {'name': 'nm-sidebar', 'repo': '~/nixos-conf/cells/common/vendor/nm-sidebar', 'branch': 'HEAD',
         'upstream': 'https://github.com/Relz/network-manager-sidebar', 'track': 'branch',
         'upstream_branch': 'main', 'strategy': 'merge', 'push': None,
         'notes': 'A git submodule of nixos-conf: the fork zyansheep/network-manager-sidebar of '
                  'Relz/network-manager-sidebar. After merging, commit the submodule bump in nixos-conf.'},
    ],
    # Packages nixos-conf patches: when upstream moves past `version`, the patches may need a rebase.
    'patches': [
        {'name': 'vicinae', 'attr': 'vicinae', 'files': ['cells/common/patches/vicinae/flatpak-app-identity.patch',
                                                         'cells/common/patches/vicinae/extension-handshake-timeout.patch'],
         'where': 'cells/nixos/profiles/graphics/niri.nix'},
        {'name': 'waybar', 'attr': 'waybar', 'files': ['cells/common/patches/waybar-power-profile-menu.patch',
                                                       'cells/common/patches/waybar-group-menu.patch',
                                                       'cells/common/patches/waybar-hover-monitor.patch',
                                                       'cells/common/patches/waybar-no-tooltips.patch',
                                                       'cells/common/patches/waybar-power-menu-processes.patch'],
         'where': 'cells/nixos/profiles/graphics/niri.nix'},
        {'name': 'swaync', 'attr': 'swaynotificationcenter',
         'files': ['cells/common/patches/swaync-background-input.patch',
                   'cells/common/patches/swaync-resident-panel.patch',
                   'cells/common/patches/swaync-warm-renderer.patch'],
         'where': 'cells/common/packages/notification-sidebar.nix'},
    ],
    # Upstream nixpkgs the patched packages are compared against, and the host whose pkgs set has them.
    'patches_upstream': 'github:NixOS/nixpkgs/nixos-unstable',
    'patches_host': 'isomorph',
    # Conflicting merges go to an agent: a T3 Code thread on this machine's T3 server.
    'agent': {'enabled': True, 't3': ['t3', '~/.nix-profile/bin/t3'], 'model': None, 'provider': None},
    'panel': {'margin': 0, 'width': 720},
    'waybar': {'signal': 13},
}

_loaded = None


def merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load(files=None, reload=False):
    global _loaded
    if _loaded is not None and not reload and files is None:
        return _loaded
    settings = DEFAULTS
    for path in files or CONFIG_FILES:
        try:
            settings = merge(settings, tomllib.loads(Path(path).read_text()))
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as e:
            print(f'update-manager: ignoring {path}: {e}', file=sys.stderr)
    if files is None:
        _loaded = settings
    return settings


def get(*keys, settings=None):
    value = settings or load()
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def hostname():
    return os.environ.get('UPDATE_MANAGER_HOST') or socket.gethostname().split('.')[0]


def flake_dir(settings=None):
    return expand(get('flake', settings=settings))


def is_ci_host(settings=None):
    return hostname() == get('ci_host', settings=settings)


def self_command():
    """How to run this program again: UPDATE_MANAGER_SELF (the package's wrapper sets it), else this Python."""
    own = os.environ.get('UPDATE_MANAGER_SELF')
    return [own] if own else [sys.executable, '-m', 'update_manager']


def remote_candidates(settings=None):
    """Ways to run update-manager on a peer, first that exists wins (see remote.shell_command)."""
    candidates = get('remote', 'command', settings=settings)
    return [candidates] if isinstance(candidates, str) else list(candidates or [])
