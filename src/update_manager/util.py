"""Small helpers shared by every part: running commands, atomic files, units."""
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

ANSI = re.compile(r'\x1b\[[0-9;]*m')
_UMASK = os.umask(0)
os.umask(_UMASK)


class CommandError(RuntimeError):
    def __init__(self, argv, code, stderr):
        self.argv, self.code, self.stderr = argv, code, stderr
        tail = (stderr or '').strip().splitlines()[-3:]
        super().__init__(f"{' '.join(map(str, argv[:4]))}: exit {code}" + (f": {' / '.join(tail)}" if tail else ''))


def run(argv, *, timeout=60, check=True, input=None, cwd=None, env=None):
    """stdout of argv as text. Raises CommandError on a non-zero exit (check),
    and on a missing program or a timeout."""
    try:
        done = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout,
                              input=input, cwd=cwd, env=env)
    except FileNotFoundError as e:
        raise CommandError(argv, 127, str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise CommandError(argv, 124, f'timed out after {timeout} s') from e
    if check and done.returncode != 0:
        raise CommandError(argv, done.returncode, done.stderr)
    return done.stdout if check else done


def write_json(path, value):
    """Replace path atomically (readers never see half a file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, separators=(',', ':'), default=str)
        # mkstemp makes 0600; other users read some of these (nixos-ci's progress.json).
        os.chmod(tmp, 0o666 & ~_UMASK)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def size_text(n):
    """1.2 GiB, 412 MiB, 35 KiB: binary units like Nix prints."""
    if n is None:
        return '?'
    n = float(n)
    sign = '-' if n < 0 else ''
    n = abs(n)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            if unit == 'B':
                return f'{sign}{n:.0f} B'
            return f'{sign}{n:.1f} {unit}' if n < 10 else f'{sign}{n:.0f} {unit}'
        n /= 1024
    return f'{sign}{n:.0f} TiB'


def duration_text(seconds):
    """45 s, 12 min, 1 h 05 min, 3 d."""
    if seconds is None:
        return '?'
    s = max(0, round(seconds))
    if s < 60:
        return f'{s} s'
    m = round(s / 60)
    if m < 60:
        return f'{m} min'
    h, m = divmod(m, 60)
    if h < 36:
        return f'{h} h {m:02d} min' if m else f'{h} h'
    return f'{round(h / 24)} d'


def ago_text(when, now=None):
    if not when:
        return 'never'
    delta = (now or time.time()) - when
    if delta < 0:
        return 'in ' + duration_text(-delta)
    if delta < 45:
        return 'just now'
    return duration_text(delta) + ' ago'


def clock_text(when, now=None):
    """12:30, or Oct 27 23:16 when it isn't today."""
    if not when:
        return '?'
    now = now or time.time()
    t, today = time.localtime(when), time.localtime(now)
    if (t.tm_year, t.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime('%H:%M', t)
    return time.strftime('%b %d %H:%M', t).replace(' 0', ' ')


def store_name(path):
    """/nix/store/<hash>-name -> name."""
    base = os.path.basename(str(path or ''))
    return base[33:] if len(base) > 33 and base[32] == '-' else base


def store_hash(path):
    base = os.path.basename(str(path or ''))
    return base[:32] if len(base) > 33 and base[32] == '-' else ''


def strip_ansi(text):
    return ANSI.sub('', text)


def expand(path):
    return Path(os.path.expandvars(os.path.expanduser(str(path))))
