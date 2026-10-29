"""Assembling state.

Each machine publishes its *own* part (own_state): its system and update, its
checkout, profiles and jobs, plus nixos-ci's results on the CI host and the
upstream checks on the machine that has the repos. The *view* (state.json) is
that plus the other machines' own parts, and is all the panel and the Waybar
module ever read.

Fast parts (files, systemctl, git status) are read on every tick; slow parts
(closure diffs, download estimates, flake metadata, upstream fetches) come from
caches the agent's slow loop fills, so `exchange` answers within a second even
when no agent runs.
"""
import os
import time
from pathlib import Path

from . import ci, config, jobs, remote, system
from .util import read_json, write_json

VERSION = 1
VIEW = config.RUNTIME / 'state.json'
VIEW_KEPT = config.CACHE / 'state.json'  # the same, kept across reboots: the widget starts with it
OWN = config.RUNTIME / 'own.json'
UPDATE_CACHE = config.CACHE / 'update.json'


def capabilities():
    etc = Path('/etc/systemd/system')
    return {
        'switch_units': (etc / 'update-manager-switch@.service').exists(),
        'rebuild': os.path.exists('/run/current-system/sw/bin/rebuild'),
        'nixos_ci': Path(config.get('nixos_ci', 'state')).is_dir(),
        'forks': config.hostname() == config.get('forks_host'),
    }


def host_state(ci_state, now=None, compute=True, settings=None):
    """This machine's system, update, checkout, profiles and jobs."""
    now = now or time.time()
    me = config.hostname()
    out = {'host': me, 'collected': now, 'label': (config.get('hosts', settings=settings) or {}).get(me, {})
           .get('label'), 'capabilities': capabilities(), 'errors': {}}
    try:
        sysinfo = system.local_system()
        sysinfo['rev'] = system.configuration_revision(sysinfo['current'])
        sysinfo['rev_source'] = 'configurationRevision' if sysinfo['rev'] else None
        if not sysinfo['rev'] and (rev := ci.rev_of(sysinfo['current'], ci_state)):
            sysinfo['rev'], sysinfo['rev_source'] = rev, 'nixos-ci'
        out['system'] = sysinfo
    except OSError as e:
        out['errors']['system'] = str(e)
        sysinfo = {}
    previous = read_json(UPDATE_CACHE)
    if ci_state:
        update = system.update_section(me, sysinfo, ci_state, settings, previous, heavy=compute)
        if compute:
            write_json(UPDATE_CACHE, update)
        out['update'] = update
    try:
        out['checkout'] = system.checkout(settings)
    except OSError as e:
        out['errors']['checkout'] = str(e)
    out['profiles'] = read_json(config.CACHE / 'profiles.json')
    out['jobs'] = jobs.recent(10, now)
    return out


def own_state(compute=True, now=None, settings=None, peer_ci=None):
    """What this machine publishes. peer_ci: the CI host's ci section, when that is another machine."""
    now = now or time.time()
    me = config.hostname()
    out = {'version': VERSION, 'host': me, 'time': now}
    ci_state = None
    if me == config.get('ci_host', settings=settings):
        ci_state = ci.collect(settings, now)
        inputs = system.flake_inputs(Path(config.get('nixos_ci', 'state', settings=settings)) /
                                     'checkout-main/flake.lock', now)
        fresh = {i['name']: i for i in (read_json(config.CACHE / 'inputs.json', {}) or {}).get('items', [])}
        for item in inputs:
            seen = fresh.get(item['name'])
            if seen and seen.get('rev') == item['rev']:
                item.update({k: seen[k] for k in ('latest', 'stale', 'check_error') if k in seen})
        ci_state['inputs'] = inputs
        ci_state['inputs_checked'] = (read_json(config.CACHE / 'inputs.json', {}) or {}).get('checked')
        out['ci'] = ci_state
        out['outbox'] = {h: remote.pending(h) for h in config.get('hosts', settings=settings) or {} if h != me}
    else:
        ci_state = peer_ci
    out['self'] = host_state(ci_state, now, compute=compute, settings=settings)
    if me == config.get('forks_host', settings=settings):
        out['forks'] = read_json(config.CACHE / 'forks.json')
        out['patches'] = read_json(config.CACHE / 'patches.json')
    return out


def published(max_age=90):
    """The agent's last own state if it is fresh, else a quick one (no slow work)."""
    own = read_json(OWN)
    if own and time.time() - own.get('time', 0) < max_age:
        return own
    peer_ci = None
    ci_host = config.get('ci_host')
    if config.hostname() != ci_host:
        peer_ci = ((remote.peer_state(ci_host) or {}).get('state') or {}).get('ci')
    return own_state(compute=False, peer_ci=peer_ci)


def view(own, peers, now=None, settings=None):
    """state.json: every machine side by side, with where each part came from."""
    now = now or time.time()
    me = own['host']
    out = {'version': VERSION, 'updated': now, 'me': me, 'ci_host': config.get('ci_host', settings=settings),
           'hosts': {}, 'peers': {}}
    out['hosts'][me] = dict(own['self'], seen=now, source='local')
    parts = [own]
    for name, entry in peers.items():
        state = (entry or {}).get('state') or {}
        link = (entry or {}).get('link') or {}
        out['peers'][name] = {'seen': (entry or {}).get('received'), **link}
        if state.get('self'):
            out['hosts'][name] = dict(state['self'], seen=(entry or {}).get('received'),
                                      source=(entry or {}).get('source', 'pushed'))
        parts.append(state)
    for part in parts:
        for key in ('ci', 'forks', 'patches', 'outbox'):
            if part.get(key) is not None and key not in out:
                out[key] = part[key]
    # A machine that never reported still gets a card.
    for name in config.get('hosts', settings=settings) or {}:
        out['hosts'].setdefault(name, {'host': name, 'source': 'none', 'seen': None,
                                       'label': (config.get('hosts', settings=settings) or {})[name].get('label')})
    order = list(config.get('hosts', settings=settings) or {})
    out['hosts'] = dict(sorted(out['hosts'].items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99))
    for name, host in out['hosts'].items():
        host['queued'] = (out.get('outbox') or {}).get(name) or []
    out['summary'] = summary(out, now)
    return out


def summary(state, now=None):
    """Counts the bar shows: machines with a newer system, packages with updates, forks behind,
    what is running."""
    now = now or time.time()
    hosts = state.get('hosts', {})
    behind = [n for n, h in hosts.items() if (h.get('update') or {}).get('state') == 'available']
    reboot = [n for n, h in hosts.items() if (h.get('system') or {}).get('reboot_pending')]
    running = []
    for n, h in hosts.items():
        for j in h.get('jobs') or []:
            if j.get('state') in ('running', 'queued'):
                running.append({'host': n, 'title': j.get('title'), 'kind': j.get('kind'),
                                'progress': j.get('progress')})
    ci_running = (state.get('ci') or {}).get('running')
    profile_updates = sum(1 for h in hosts.values()
                          for i in ((h.get('profiles') or {}).get('items') or []) if i.get('update'))
    forks_behind = [i['name'] for i in ((state.get('forks') or {}).get('items') or [])
                    if i.get('status') not in ('current', None, 'missing')]
    offline = [n for n, h in hosts.items() if n != state.get('me') and (not h.get('seen') or now - h['seen'] > 600)]
    return {'behind': behind, 'reboot': reboot, 'running': running, 'ci_running': bool(ci_running),
            'ci': ci_running, 'profile_updates': profile_updates, 'forks_behind': forks_behind,
            'offline': offline}


def publish(state):
    write_json(VIEW, state)
    write_json(VIEW_KEPT, state)


def read_view():
    """The freshest view there is: the live one, else the one kept from before (marked stale)."""
    live = read_json(VIEW)
    if live:
        return live
    kept = read_json(VIEW_KEPT)
    if kept:
        kept['kept'] = True
    return kept
