"""nixos-ci on the CI host: its last results, whether it is building now (and how far
along), its run history from the journal, the poll timer and an ETA.

Everything here is read-only: status files in /var/lib/nixos-ci, `systemctl show`
and `journalctl`. Starting builds is jobs.py's (through polkit, as nixos-ci allows wheel).
"""
import json
import re
import statistics
import time
from pathlib import Path

from . import config
from .util import CommandError, read_json, run

BUILD_START = re.compile(r'^building ([0-9a-f]{12}): (.*)$')
HOST_RESULT = re.compile(r'^(\S+) (ok|FAILED)(?: (/nix/store/\S+))?$')
GREP = r'^(building [0-9a-f]{12}|\S+ (ok|FAILED)( |$))'


def parse_status(text):
    """main.status / update.status -> {'rev', 'built', 'minutes', 'hosts': {h: {'ok', 'path'}}, 'lock'}."""
    out = {'rev': None, 'built': None, 'minutes': None, 'hosts': {}, 'lock': False}
    for line in (text or '').splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == 'rev' and len(parts) > 1:
            out['rev'] = parts[1]
        elif parts[0] == 'built' and len(parts) > 1:
            out['built'] = iso_time(parts[1])
            if len(parts) >= 4 and parts[2] == 'in':
                try:
                    out['minutes'] = int(parts[3])
                except ValueError:
                    pass
        elif parts[0] == 'lock':
            out['lock'] = True
        elif len(parts) >= 2 and parts[1] in ('ok', 'FAILED'):
            out['hosts'][parts[0]] = {'ok': parts[1] == 'ok', 'path': parts[2] if len(parts) > 2 else None}
    return out


def iso_time(text):
    import datetime
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def roots(state_dir, mode='main'):
    """GC roots nixos-ci keeps: {host: [{'path', 'rev12', 'stamp'}]} newest first. The names are
    <YYYYmmdd-HHMMSS>-<rev12>, so they map a store path back to the commit it was built from."""
    out = {}
    base = Path(state_dir) / 'roots' / mode
    try:
        hosts = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return out
    for host_dir in hosts:
        items = []
        for link in host_dir.iterdir():
            name = link.name
            m = re.match(r'^(\d{8}-\d{6})-([0-9a-f]{12})$', name)
            if not m:
                continue
            try:
                target = str(link.readlink())
            except OSError:
                continue
            items.append({'path': target, 'rev12': m[2], 'stamp': stamp_time(m[1])})
        out[host_dir.name] = sorted(items, key=lambda i: i['stamp'] or 0, reverse=True)
    return out


def stamp_time(stamp):
    try:
        return time.mktime(time.strptime(stamp, '%Y%m%d-%H%M%S'))
    except ValueError:
        return None


def unit_states(units):
    """{unit: {'active', 'sub', 'since', 'result'}} from systemctl show."""
    try:
        text = run(['systemctl', 'show', *units, '--timestamp=unix', '-p',
                    'Id,ActiveState,SubState,ExecMainStartTimestamp,ActiveEnterTimestamp,Result,'
                    'NextElapseUSecRealtime,LastTriggerUSec'], timeout=10)
    except CommandError:
        return {}
    return parse_show(text)


def parse_show(text):
    out, current = {}, {}
    for line in text.splitlines() + ['']:
        if not line.strip():
            if current.get('Id'):
                out[current['Id']] = {
                    'active': current.get('ActiveState'), 'sub': current.get('SubState'),
                    'result': current.get('Result'),
                    'since': unix(current.get('ExecMainStartTimestamp')) or unix(current.get('ActiveEnterTimestamp')),
                    'next': unix(current.get('NextElapseUSecRealtime')),
                    'last': unix(current.get('LastTriggerUSec')),
                }
            current = {}
            continue
        key, _, value = line.partition('=')
        current[key] = value
    return out


def unix(value):
    """'@1793302200' (systemctl --timestamp=unix) -> 1793302200.0."""
    if value and value.startswith('@'):
        try:
            return float(value[1:])
        except ValueError:
            return None
    return None


def history(units, days=30, journal=None):
    """Past builds that built something (not 'already built' polls): newest first,
    [{'rev12', 'subject', 'start', 'end', 'seconds', 'hosts': {h: ok}, 'unit'}]."""
    if journal is None:
        args = ['journalctl', '--no-pager', '-o', 'json', '--output-fields=MESSAGE,_SYSTEMD_UNIT',
                f'--since=-{days}d', '-g', GREP]
        for u in units:
            args += ['-u', u]
        try:
            journal = run(args, timeout=20, check=False).stdout
        except CommandError:
            return []
    runs, current = [], None
    for line in journal.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get('MESSAGE')
        if isinstance(message, list):  # journald sends non-UTF-8 messages as byte arrays
            message = bytes(message).decode(errors='replace')
        message = (message or '').strip()
        when = int(entry.get('__REALTIME_TIMESTAMP', 0)) / 1e6
        if m := BUILD_START.match(message):
            if current:
                runs.append(current)
            current = {'rev12': m[1], 'subject': m[2], 'start': when, 'end': None, 'hosts': {},
                       'unit': entry.get('_SYSTEMD_UNIT')}
        elif (m := HOST_RESULT.match(message)) and current:
            current['hosts'][m[1]] = m[2] == 'ok'
            current['end'] = when
    if current:
        runs.append(current)
    for r in runs:
        r['seconds'] = (r['end'] - r['start']) if r['end'] else None
    return sorted(runs, key=lambda r: r['start'], reverse=True)


def typical_seconds(runs):
    done = [r['seconds'] for r in runs if r.get('seconds')]
    return statistics.median(done[:10]) if done else None


def collect(settings=None, now=None, journal=None):
    """The CI section of the CI host's state."""
    now = now or time.time()
    cfg = config.get('nixos_ci', settings=settings)
    state_dir = Path(cfg['state'])
    out = {'available': state_dir.is_dir(), 'collected': now}
    if not out['available']:
        return out
    for mode in ('main', 'update'):
        try:
            out[mode] = parse_status((state_dir / f'{mode}.status').read_text())
        except OSError:
            out[mode] = None
    out['roots'] = roots(state_dir)
    units = unit_states([*cfg['units'], cfg['timer']])
    timer = units.pop(cfg['timer'], {})
    out['next_poll'], out['last_poll'] = timer.get('next'), timer.get('last')
    out['units'] = units
    runs = history(cfg['units'], journal=journal)
    out['history'] = runs[:12]
    out['typical_s'] = typical_seconds(runs)
    out['running'] = running(units, runs, state_dir, now, out['typical_s'])
    return out


def running(units, runs, state_dir, now, typical):
    """The build in progress, if one is: its unit, since when, what commit (once the journal says),
    and progress from progress.json when nixos-ci writes one (the nixlog hook)."""
    busy = [(u, s) for u, s in units.items() if s.get('active') in ('activating', 'active', 'deactivating')
            and s.get('sub') not in ('exited',)]
    if not busy:
        return None
    unit, state = busy[0]
    since = state.get('since') or now
    info = {'unit': unit, 'since': since, 'elapsed': now - since, 'mode': 'update' if 'update' in unit else 'main'}
    latest = runs[0] if runs else None
    if latest and latest['start'] >= since - 5:
        info['rev12'], info['subject'] = latest['rev12'], latest['subject']
        info['phase'] = 'building'
    else:
        # The poll unit runs every 15 minutes and mostly finds nothing new: until it says
        # "building", it is fetching or evaluating.
        info['phase'] = 'checking'
    progress = read_json(state_dir / 'progress.json')
    if progress and progress.get('started', 0) >= since - 5:
        info['progress'] = progress
        if progress.get('eta'):
            info['eta_s'] = progress['eta']['seconds']
            info['eta_quality'] = progress['eta']['quality']
    if 'eta_s' not in info and typical and info['phase'] == 'building':
        info['eta_s'] = max(0, typical - info['elapsed'])
        info['eta_quality'] = 'typical'
    return info


def rev_of(path, ci_state):
    """The commit (12 hex digits) a system path was built from, if nixos-ci built it."""
    for items in (ci_state or {}).get('roots', {}).values():
        for item in items:
            if item['path'] == path:
                return item['rev12']
    main = (ci_state or {}).get('main') or {}
    for h in main.get('hosts', {}).values():
        if h.get('path') == path and main.get('rev'):
            return main['rev'][:12]
    return None


def target_for(host, ci_state):
    """The newest successful CI build for host: {'path', 'rev', 'built'} or None."""
    main = (ci_state or {}).get('main') or {}
    entry = main.get('hosts', {}).get(host)
    if entry and entry.get('ok') and entry.get('path'):
        return {'path': entry['path'], 'rev': main.get('rev'), 'built': main.get('built')}
    items = (ci_state or {}).get('roots', {}).get(host) or []
    if items:
        return {'path': items[0]['path'], 'rev': items[0]['rev12'], 'built': items[0]['stamp']}
    return None
