"""Words for the state: shared by `update-manager status`, the panel and the tooltip."""
import time

from .system import version_text
from .util import ago_text, clock_text, duration_text, size_text, store_name


def host_headline(name, host, now=None):
    """(text, kind): 'Up to date' / '2 commits behind' / 'Switching…' / 'Not heard from'."""
    now = now or time.time()
    for job in host.get('jobs') or []:
        if job.get('state') in ('running', 'queued') and job.get('kind') in ('switch', 'boot', 'rebuild'):
            return {'switch': 'Switching…', 'boot': 'Setting the boot entry…', 'rebuild': 'Rebuilding…'}[job['kind']], 'busy'
    if host.get('queued'):
        return f"{host['queued'][0]['action']} queued until it checks in", 'busy'
    if host.get('source') == 'none':
        return 'No report yet', 'offline'
    update = host.get('update') or {}
    state = update.get('state')
    if state == 'available':
        behind = update.get('behind')
        return (f'{behind} commit{"s" if behind != 1 else ""} behind' if behind else 'A newer build is ready'), 'updates'
    if state == 'current':
        if (host.get('system') or {}).get('reboot_pending'):
            return 'Up to date · reboot pending', 'reboot'
        return 'Up to date', 'current'
    target = (update.get('target_rev') or '?')[:8]
    if state == 'local':
        return f'Local build · CI has {target}', 'unknown'
    if state == 'ahead':
        return f'Ahead of CI ({target})', 'unknown'
    if state == 'no-ci':
        return 'No CI build for it', 'unknown'
    return 'Unknown', 'unknown'


def seen_text(host, me, now=None):
    if host.get('host') == me or host.get('source') == 'local':
        return 'this machine'
    if host.get('source') == 'mock':
        return 'mock data'
    if not host.get('seen'):
        return 'never seen'
    return f"seen {ago_text(host['seen'], now)}"


def running_text(host):
    s = host.get('system') or {}
    gen = f"gen {s['generation']} · " if s.get('generation') else ''
    rev = f"{s['rev']} · " if s.get('rev') else ''
    switched = f"switched {clock_text(s['switched'])}" if s.get('switched') else ''
    return f"{rev}{gen}{switched}".strip(' ·') or store_name(s.get('current'))


def change_text(change):
    old, new = change['from'], change['to']
    if old is not None and new is not None and old != new:
        return f"{version_text(old)} → {version_text(new)}"
    if old is None:
        return f"new {', '.join(new)}" if new else 'new'
    if new is None:
        return 'removed'
    return ('+' if change['delta'] > 0 else '') + size_text(change['delta'])


def estimate_text(update):
    dl = update.get('download') or {}
    est = update.get('estimate') or {}
    if not dl:
        return 'working out the download…' if update.get('pending') else ''
    if dl.get('error'):
        return f"no estimate: {dl['error']}"
    parts = []
    if dl.get('download'):
        approx = '≤ ' if dl.get('approximate') else ''
        parts.append(f"{approx}{size_text(dl['download'])} to download ({dl.get('paths', 0)} paths, "
                     f"{size_text(dl.get('unpacked'))} unpacked)")
    else:
        parts.append('nothing to download')
    if dl.get('builds'):
        parts.append(f"{dl['builds']} to build")
    if est.get('seconds') is not None:
        parts.append(f"~{duration_text(est['seconds'])}" + ('' if est.get('measured') else ' (est.)'))
    return ' · '.join(parts)


def ci_text(ci, now=None):
    now = now or time.time()
    if not ci or not ci.get('available'):
        return 'nixos-ci: no report'
    run = ci.get('running')
    if run:
        p = run.get('progress') or {}
        b = p.get('builds') or {}
        bits = [f"Building {run.get('rev12') or ''}".strip() if run.get('phase') == 'building' else 'Checking origin/main']
        if b.get('expected'):
            bits.append(f"{b.get('done', 0)}/{b['expected']} built")
        if run.get('eta_s') is not None:
            bits.append(f"~{duration_text(run['eta_s'])} left")
        bits.append(f"for {duration_text(run.get('elapsed'))}")
        return ' · '.join(bits)
    main = ci.get('main') or {}
    hosts = main.get('hosts') or {}
    ok = all(h.get('ok') for h in hosts.values()) if hosts else False
    built = f"built {clock_text(main['built'])}" if main.get('built') else ''
    took = f"in {main['minutes']} min" if main.get('minutes') is not None else ''
    return ' '.join(filter(None, [f"{(main.get('rev') or '?')[:12]}", 'ok' if ok else 'FAILED', built, took]))


def summary(state):
    if not state:
        return 'no state yet (is the agent running?)'
    now = time.time()
    lines = []
    for name, host in state.get('hosts', {}).items():
        headline, _ = host_headline(name, host, now)
        lines.append(f"{name} ({seen_text(host, state.get('me'), now)}): {headline}")
        if host.get('system'):
            lines.append(f"  running {running_text(host)}")
        update = host.get('update') or {}
        if update.get('state') == 'available':
            for c in update.get('commits') or []:
                lines.append(f"  + {c['rev'][:8]} {c['subject']}")
            if update.get('diff'):
                d = update['diff']
                lines.append(f"  {d['upgraded']} upgraded, {d['added']} added, {d['removed']} removed, "
                             f"{size_text(d['size_delta'])}")
                for c in d['changes'][:6]:
                    lines.append(f"    {c['name']}: {change_text(c)}")
            lines.append(f"  {estimate_text(update)}")
    lines.append(f"CI: {ci_text(state.get('ci'), now)}")
    for item in (state.get('forks') or {}).get('items', []):
        lines.append(f"fork {item['name']}: {item.get('status')} "
                     f"(+{item.get('ahead', '?')} −{item.get('behind', '?')} vs {item.get('upstream', '?')})")
    return '\n'.join(lines)
