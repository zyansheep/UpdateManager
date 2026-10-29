"""Progress from Nix's `--log-format internal-json` log, for ETAs.

`update-manager nixlog --progress FILE [--history FILE]` reads that log on
stdin (nixos-ci's `nix build`, the switch unit's `nix-store --realise`),
prints the human lines (what Nix would print without it) to stdout for the
journal, and keeps FILE up to date with builds done/expected, what is
building and for how long, bytes copied and downloaded. At the end it adds
every finished build's duration to the history file, which later ETAs use.

It never exits before its input ends: the build it watches must not get a
broken pipe because of it.
"""
import json
import re
import statistics
import sys
import time

from .util import read_json, store_name, write_json

# Nix's ActivityType and ResultType (src/libutil/logging.hh).
ACT_COPY_PATH, ACT_FILE_TRANSFER, ACT_REALISE, ACT_COPY_PATHS, ACT_BUILDS, ACT_BUILD = 100, 101, 102, 103, 104, 105
ACT_SUBSTITUTE = 108
RES_BUILD_LOG_LINE, RES_SET_PHASE, RES_PROGRESS, RES_SET_EXPECTED = 101, 104, 105, 106

PLAN_BUILD = re.compile(r'^these (\d+) derivations? will be built:')
PLAN_FETCH = re.compile(r'^these (\d+) paths? will be fetched \(([\d.]+) (\w+) download, ([\d.]+) (\w+) unpacked\)')
UNITS = {'B': 1, 'KiB': 1024, 'MiB': 1024 ** 2, 'GiB': 1024 ** 3, 'TiB': 1024 ** 4}
VERSION = re.compile(r'-\d[^-]*$')
DEFAULT_BUILD_S = 20.0  # A derivation nothing is known about: most are small (units, scripts, wrappers).


def drv_name(drv):
    name = store_name(drv)
    return name[:-4] if name.endswith('.drv') else name


def pname(name):
    """firefox-145.0 -> firefox (for history lookups across versions)."""
    return VERSION.sub('', name)


class Progress:
    """The state of one Nix invocation's log."""

    def __init__(self, now=None):
        self.now = now or (lambda: time.time())
        self.started = self.now()
        self.activities = {}  # id -> {'type', 'name', 'start', 'phase'}
        self.builds = {'done': 0, 'expected': 0, 'running': 0, 'failed': 0}
        self.copies = {'done': 0, 'expected': 0}
        self.copy_bytes = {}  # actCopyPath id -> (done, expected)
        self.transfer_bytes = {}  # actFileTransfer id -> (done, expected)
        self.expected_copy_bytes = 0
        self.planned = []  # derivation names from "these N derivations will be built:"
        self.planned_fetch = None  # {'paths', 'download', 'unpacked'} from "these N paths will be fetched"
        self._plan = None
        self.finished = []  # (name, seconds)
        self.errors = []
        self.last_line = ''
        self.log_tail = {}  # build id -> last lines

    # --- events ---------------------------------------------------------------
    def feed(self, line):
        """One line of stderr; returns the human text to print, if any."""
        if not line.startswith('@nix '):
            text = line.rstrip('\n')
            if text:
                self.message({'msg': text, 'level': 3})  # plain Nix output: the plan lines still count
            return text or None
        try:
            event = json.loads(line[5:])
        except ValueError:
            return None
        action = event.get('action')
        if action == 'msg':
            return self.message(event)
        if action == 'start':
            return self.start(event)
        if action == 'stop':
            self.stop(event.get('id'))
        elif action == 'result':
            self.result(event)
        return None

    def message(self, event):
        text = event.get('msg') or ''
        plain = re.sub(r'\x1b\[[0-9;]*m', '', text)
        if m := PLAN_BUILD.match(plain):
            self._plan = 'build'
        elif m := PLAN_FETCH.match(plain):
            self._plan = 'fetch'
            self.planned_fetch = {'paths': int(m[1]), 'download': float(m[2]) * UNITS.get(m[3], 1),
                                  'unpacked': float(m[4]) * UNITS.get(m[5], 1)}
        elif plain.startswith('  /nix/store/') and self._plan == 'build':
            self.planned.append(drv_name(plain.strip()))
        else:
            self._plan = None
        if event.get('level', 3) <= 0 or plain.startswith('error:'):
            self.errors = (self.errors + [plain.splitlines()[0] if plain else ''])[-10:]
        if event.get('level', 3) <= 3:
            self.last_line = plain.splitlines()[0] if plain else self.last_line
            return plain
        return None

    def start(self, event):
        kind, fields = event.get('type'), event.get('fields') or []
        entry = {'type': kind, 'start': self.now(), 'phase': None, 'name': None}
        if kind == ACT_BUILD and fields:
            entry['name'] = drv_name(fields[0])
            entry['machine'] = fields[1] if len(fields) > 1 else ''
        elif kind in (ACT_SUBSTITUTE, ACT_COPY_PATH) and fields:
            entry['name'] = store_name(fields[0])
        self.activities[event.get('id')] = entry
        text = event.get('text') or ''
        if kind in (ACT_BUILD, ACT_SUBSTITUTE) and text:
            self.last_line = text
            return text
        return None

    def stop(self, ident):
        entry = self.activities.pop(ident, None)
        if entry and entry['type'] == ACT_BUILD and entry['name']:
            self.finished.append((entry['name'], self.now() - entry['start']))
            self.log_tail.pop(ident, None)

    def result(self, event):
        kind, fields, ident = event.get('type'), event.get('fields') or [], event.get('id')
        entry = self.activities.get(ident)
        if kind == RES_PROGRESS and len(fields) >= 2:
            if entry is None:
                return
            if entry['type'] == ACT_BUILDS and len(fields) >= 4:
                self.builds = {'done': fields[0], 'expected': fields[1], 'running': fields[2], 'failed': fields[3]}
            elif entry['type'] == ACT_COPY_PATHS:
                self.copies = {'done': fields[0], 'expected': fields[1]}
            elif entry['type'] == ACT_COPY_PATH:
                self.copy_bytes[ident] = (fields[0], fields[1])
            elif entry['type'] == ACT_FILE_TRANSFER:
                self.transfer_bytes[ident] = (fields[0], fields[1])
        elif kind == RES_SET_EXPECTED and len(fields) >= 2 and entry is not None:
            if fields[0] == ACT_COPY_PATH and entry['type'] in (ACT_REALISE, ACT_COPY_PATHS):
                self.expected_copy_bytes = max(self.expected_copy_bytes, fields[1])
        elif kind == RES_SET_PHASE and fields and entry is not None:
            entry['phase'] = fields[0]
        elif kind == RES_BUILD_LOG_LINE and fields and entry is not None:
            tail = self.log_tail.setdefault(ident, [])
            tail.append(str(fields[0])[:300])
            del tail[:-5]

    # --- summary --------------------------------------------------------------
    def running(self):
        now = self.now()
        return sorted(({'name': a['name'], 'elapsed': now - a['start'], 'phase': a['phase'],
                        'machine': a.get('machine') or ''}
                       for a in self.activities.values() if a['type'] == ACT_BUILD and a['name']),
                      key=lambda r: -r['elapsed'])

    def snapshot(self, history=None, jobs=2):
        copied = sum(d for d, _ in self.copy_bytes.values())
        downloaded = sum(d for d, _ in self.transfer_bytes.values() if d > 4096)
        running = self.running()
        builds = dict(self.builds)
        # Nix's own count of expected builds runs one high while goals are being created;
        # the plan it printed ("these N derivations will be built") is the real number.
        if self.planned and len(self.planned) >= builds['done']:
            builds['expected'] = len(self.planned)
        snap = {
            'updated': self.now(), 'started': self.started,
            'builds': builds, 'copies': dict(self.copies),
            'bytes': {'copied': copied, 'expected': self.expected_copy_bytes, 'downloaded': downloaded},
            'running': running[:6], 'planned': len(self.planned), 'fetch': self.planned_fetch,
            'errors': self.errors[-3:], 'last': self.last_line[:200],
        }
        snap['eta'] = eta(snap, self.planned, [n for n, _ in self.finished], history or {}, jobs)
        return snap


def typical(history, name):
    """Seconds a build of name took before: this name, else this package at another version."""
    if not history:
        return None
    if name in history.get('names', {}):
        return history['names'][name]
    return history.get('pnames', {}).get(pname(name))


def eta(snap, planned, finished, history, jobs=2):
    """Seconds left for the builds (None if nothing is known), and how sure: 'history' when
    every remaining build has a recorded duration, 'partial', or 'guess'."""
    builds = snap['builds']
    left_count = max(0, builds['expected'] - builds['done'] - builds['failed'])
    if not left_count and not snap['running']:
        return None
    default = history.get('median') or DEFAULT_BUILD_S
    known = 0
    work = 0.0
    running_names = {r['name'] for r in snap['running']}
    for r in snap['running']:
        t = typical(history, r['name'])
        known += t is not None
        t = t if t is not None else max(default, r['elapsed'] * 1.5)
        work += max(t - r['elapsed'], 0.1 * t)
    done = set(finished)
    waiting = [n for n in planned if n not in done and n not in running_names]
    waiting_count = max(0, left_count - len(snap['running']))
    for name in waiting[:waiting_count]:
        t = typical(history, name)
        known += t is not None
        work += t if t is not None else default
    work += default * max(0, waiting_count - len(waiting))
    total = len(snap['running']) + waiting_count
    seconds = work / max(1, min(jobs, total or 1))
    # Downloads still to come, at the rate seen so far.
    b = snap['bytes']
    elapsed = snap['updated'] - snap['started']
    if b['expected'] > b['copied'] > 0 and elapsed > 5:
        seconds = max(seconds, (b['expected'] - b['copied']) / (b['copied'] / elapsed))
    quality = 'history' if known == total and total else ('partial' if known else 'guess')
    return {'seconds': round(seconds), 'quality': quality}


def merge_history(history, finished, keep=4000):
    """Fold finished (name, seconds) into the history: the last 5 durations per name, their median,
    the same by package name, and the median over everything."""
    history = history or {}
    runs = history.setdefault('runs', {})
    for name, seconds in finished:
        if seconds < 0.5:  # Already built, substituted or failed at once: says nothing about building.
            continue
        runs[name] = (runs.get(name, []) + [round(seconds, 1)])[-5:]
    if len(runs) > keep:
        for name in list(runs)[:len(runs) - keep]:
            del runs[name]
    names = {n: statistics.median(v) for n, v in runs.items() if v}
    by_pname = {}
    for n, v in names.items():
        by_pname.setdefault(pname(n), []).append(v)
    history['names'] = names
    history['pnames'] = {p: statistics.median(v) for p, v in by_pname.items()}
    history['median'] = statistics.median(names.values()) if names else DEFAULT_BUILD_S
    return history


def main(argv):
    """nixlog --progress FILE [--history FILE] [--jobs N] [--quiet]"""
    args = {'--progress': None, '--history': None, '--jobs': '2'}
    quiet = '--quiet' in argv
    argv = [a for a in argv if a != '--quiet']
    it = iter(argv)
    for a in it:
        if a in args:
            args[a] = next(it, None)
        else:
            print(f'nixlog: unknown argument {a}', file=sys.stderr)
    out = args['--progress']
    history_path = args['--history']
    jobs = int(args['--jobs'] or 2)
    history = read_json(history_path, {}) if history_path else {}
    progress = Progress()
    last_write = 0.0
    for line in sys.stdin:
        try:
            text = progress.feed(line)
            if text and not quiet:
                print(text, flush=True)
            now = time.time()
            if out and now - last_write >= 1.0:
                write_json(out, dict(progress.snapshot(history, jobs), state='running'))
                last_write = now
        except Exception as e:  # noqa: BLE001 - never stop reading: the build would get EPIPE
            print(f'update-manager nixlog: {e}', file=sys.stderr)
    try:
        if out:
            write_json(out, dict(progress.snapshot(history, jobs), state='finished'))
        if history_path and progress.finished:
            write_json(history_path, merge_history(read_json(history_path, {}), progress.finished))
    except Exception as e:  # noqa: BLE001
        print(f'update-manager nixlog: {e}', file=sys.stderr)
    return 0
