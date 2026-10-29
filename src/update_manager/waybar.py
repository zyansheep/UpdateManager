"""The Waybar module: `update-manager waybar` prints a JSON line whenever the label changes.

It only reads state.json (the agent writes it), so it starts at once and costs
nothing; it imports neither GTK nor anything slow. Glyphs are Font Awesome
(the bar's font): check (all current), rotate (updates, with how many
machines), hammer (nixos-ci building, with its progress), download (a switch
running), triangle (something failed).
"""
import json
import sys
import time

from . import collect
from .util import duration_text

GLYPHS = {'current': '', 'updates': '', 'building': '', 'switching': '',
          'failed': '', 'unknown': ''}
STALE = 120  # s: an older state.json means the agent isn't running.


def percent(progress):
    if not progress:
        return None
    b = progress.get('builds') or {}
    if b.get('expected'):
        return round(100 * b.get('done', 0) / b['expected'])
    by = progress.get('bytes') or {}
    if by.get('expected'):
        return round(100 * by.get('copied', 0) / by['expected'])
    return None


def label(state, now=None):
    now = now or time.time()
    if not state:
        return {'text': GLYPHS['unknown'], 'class': ['unknown'], 'tooltip': 'update-manager: no state yet'}
    s = state.get('summary') or {}
    classes, lines = [], []
    stale = state.get('kept') or now - state.get('updated', 0) > STALE
    running = s.get('running') or []
    switching = [r for r in running if r.get('kind') in ('switch', 'boot', 'rebuild', 'profile-upgrade')]
    failed = [(n, j) for n, h in (state.get('hosts') or {}).items() for j in (h.get('jobs') or [])[:1]
              if j.get('state') in ('failed', 'lost') and now - (j.get('finished') or 0) < 3600]
    text = GLYPHS['current']
    if switching:
        p = percent(switching[0].get('progress'))
        text = GLYPHS['switching'] + (f' {p}%' if p is not None else '')
        classes.append('switching')
        lines.append(f"{switching[0]['host']}: {switching[0]['title']}")
    elif s.get('ci_running'):
        ci = s.get('ci') or {}
        p = percent(ci.get('progress'))
        eta = ci.get('eta_s')
        text = GLYPHS['building'] + (f' {p}%' if p is not None else '')
        classes.append('building')
        lines.append(f"CI {ci.get('phase', 'building')} {ci.get('rev12') or ''}"
                     + (f' · ~{duration_text(eta)} left' if eta else ''))
    elif failed:
        text = GLYPHS['failed']
        classes.append('failed')
        lines += [f"{n}: {j.get('title')} failed" for n, j in failed]
    elif s.get('behind'):
        text = f"{GLYPHS['updates']} {len(s['behind'])}"
        classes.append('updates')
    else:
        classes.append('current')
    for name in s.get('behind') or []:
        update = state['hosts'][name].get('update') or {}
        behind = update.get('behind')
        dl = (update.get('download') or {}).get('download')
        lines.append(f"{name}: " + (f"{behind} commits behind" if behind else 'a newer build')
                     + (f" · {dl / 2**20:.0f} MiB" if dl else ''))
    if s.get('reboot'):
        classes.append('reboot')
        lines.append('Reboot pending: ' + ', '.join(s['reboot']))
    if s.get('profile_updates'):
        classes.append('profiles')
        lines.append(f"{s['profile_updates']} profile packages have updates")
    if s.get('forks_behind'):
        classes.append('forks')
        lines.append('Upstream changes: ' + ', '.join(s['forks_behind']))
    if s.get('offline'):
        lines.append('Not heard from: ' + ', '.join(s['offline']))
    if stale:
        classes.append('stale')
        lines.append('(the agent is not running: this is the last state it saw)')
    return {'text': text, 'class': classes, 'tooltip': '\n'.join(lines) or 'Everything is up to date',
            'alt': classes[0] if classes else 'current'}


def main(argv):
    if argv and argv[0] == '--once':
        print(json.dumps(label(collect.read_view())))
        return 0
    last = None
    while True:
        try:
            line = json.dumps(label(collect.read_view()))
        except Exception as e:  # noqa: BLE001 - Waybar would show nothing; show why instead
            line = json.dumps({'text': GLYPHS['failed'], 'class': ['failed'], 'tooltip': str(e)})
        if line != last:
            print(line, flush=True)
            last = line
        time.sleep(2)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
