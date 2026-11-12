"""Asking the user to approve a rebuild: `update-manager ask-rebuild REASON [REBUILD ARGS...]`.

For coding agents, which may not switch the system on their own (and, started with NoNewPrivileges,
can't sudo). The command starts an ask-rebuild job: a transient user unit like any job, so it runs
outside the agent's restrictions. The job stages the checkout (`git add .`, as `nrb` does), builds
the system without root (`ask.preview`, nixos-conf's `rebuild --build-only`) and shows a full-screen
window (approve.py): the agent's reason, the closure diff, the commits since the running system and
the uncommitted files, a message box, Approve and Deny. On Approve it runs `ask.switch` (`sudo -n
rebuild`) with the agent's rebuild args. If the checkout changed while the window was open, it builds
and asks again, so what gets switched is what was shown.

The command follows the job's log and exits with EXIT[outcome]; the user's message is printed last.
"""
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time

from . import config, jobs, system
from .util import CommandError, expand, run, write_json

EXIT = {'switched': 0, 'current': 0, 'denied': 77, 'timeout': 75, 'no-window': 69}
USAGE = """\
usage: update-manager ask-rebuild REASON [REBUILD ARGS...]

Asks the user, in a full-screen window, to switch this machine to the checkout (`git add .`, then
`sudo rebuild REBUILD ARGS`). The window shows REASON, what changes, and a message box for the user's
answer. Blocks until the user answers and the switch ends; if this command is interrupted the job
goes on (`update-manager job list`, `update-manager job log ID`).
Exit: 0 switched (or nothing to switch), 77 denied, 75 no answer in time, 69 no window could be
shown, 1 the build or the switch failed.
"""


def command(key):
    value = config.get('ask', key)
    return [expand(str(a)) for a in (shlex.split(value) if isinstance(value, str) else value)]


def say(log, text):
    log.write(f'ask-rebuild: {text}\n'.encode())
    log.flush()


def stage(repo):
    """`git add .` (flakes see only what git tracks); the staged tree, to tell whether it changed."""
    run(['git', '-C', repo, 'add', '.'], timeout=60)
    return run(['git', '-C', repo, 'write-tree'], timeout=30).strip()


def request(job, args, repo, current, new, note=None):
    """What the window shows."""
    out = {'reason': args['reason'], 'from': args.get('from'), 'args': args.get('args') or [],
           'host': config.hostname(), 'current': current, 'new': new, 'note': note,
           'timeout': int(config.get('ask', 'timeout') or 900)}
    try:
        out['diff'] = system.diff_closures(system.path_infos(current), system.path_infos(new))
    except (CommandError, ValueError, OSError) as e:
        out['diff_error'] = str(e)
    head = run(['git', '-C', repo, 'rev-parse', 'HEAD'], timeout=15, check=False).stdout.strip()
    running = system.configuration_revision(current)
    out['running_rev'] = running
    out['commits'] = system.commits_between((running or '').removesuffix('-dirty'), head) if running else None
    status = run(['git', '-C', repo, 'diff', '--cached', '--name-status', 'HEAD'], timeout=30, check=False)
    out['uncommitted'] = status.stdout.splitlines()[:200]
    return out


def show_window(path, timeout):
    """The user's answer: {'decision': 'approve'|'deny'|'timeout', 'message': str}, or ActionError."""
    window = os.environ.get('UPDATE_MANAGER_ASK_WINDOW')
    argv = shlex.split(window) if window else [*config.self_command(), 'approve-window']
    try:
        done = subprocess.run([*argv, str(path)], capture_output=True, text=True, timeout=timeout + 60)
    except subprocess.TimeoutExpired:
        return {'decision': 'timeout', 'message': ''}
    except OSError as e:
        raise jobs.ActionError(f'could not show the window: {e}') from e
    answer = None
    for line in reversed(done.stdout.splitlines()):
        try:
            answer = json.loads(line)
            break
        except ValueError:
            continue
    if done.returncode != 0 or not isinstance(answer, dict) or answer.get('decision') not in (
            'approve', 'deny', 'timeout'):
        tail = ' / '.join(done.stderr.strip().splitlines()[-3:])
        raise jobs.ActionError(f'could not show the window (exit {done.returncode}){": " + tail if tail else ""}')
    return {'decision': answer['decision'], 'message': str(answer.get('message') or '')[:4000]}


def do_ask_rebuild(job, args, log):
    repo = config.flake_dir()
    work = config.RUNTIME / 'ask' / job['id']
    work.mkdir(parents=True, exist_ok=True)
    result = work / 'result'
    current = os.path.realpath(config.get('ask', 'current') or '/run/current-system')
    rebuild_args = args.get('args') or []
    say(log, f"request: {args['reason']}" + (f" (from {args['from']})" if args.get('from') else ''))
    if rebuild_args:
        say(log, f'rebuild args: {shlex.join(rebuild_args)}')
    note = None
    try:
        while True:
            tree = stage(repo)
            say(log, 'building the system without root, to show what changes')
            job.update(outcome='building', tail='building to show what changes')
            jobs.save(job)
            result.unlink(missing_ok=True)
            try:
                jobs.stream(job, [*command('preview'), *rebuild_args], log, cwd=work)
            except jobs.ActionError as e:
                job['outcome'] = 'build-failed'
                raise jobs.ActionError(f'the build failed, nothing was asked: {e}') from e
            new = os.path.realpath(result)
            if new == current:
                job['outcome'] = 'current'
                say(log, f'{new} is the running system already')
                return 'nothing to switch: the build is the running system'
            path = work / 'request.json'
            write_json(path, request(job, args, repo, current, new, note))
            say(log, 'waiting for the user to answer (full-screen window)')
            job.update(outcome='asking', tail='waiting for your answer', progress=None)
            jobs.save(job)
            try:
                answer = show_window(path, int(config.get('ask', 'timeout') or 900))
            except jobs.ActionError:
                job['outcome'] = 'no-window'
                raise
            job['answer'] = answer
            message = f": {answer['message']}" if answer['message'] else ''
            if answer['decision'] == 'timeout':
                job['outcome'] = 'timeout'
                raise jobs.ActionError('no answer in time, not switching')
            if answer['decision'] != 'approve':
                job['outcome'] = 'denied'
                raise jobs.ActionError(f'denied{message}')
            if stage(repo) != tree:
                note = 'The checkout changed while you were asked. This is the new build.'
                say(log, 'the checkout changed while the window was open; building again')
                continue
            break
        say(log, f'approved{message}; switching')
        job.update(outcome='switching', tail='switching')
        jobs.save(job)
        try:
            done = jobs.stream(job, [*command('switch'), *rebuild_args], log, done=f'approved and switched{message}')
        except jobs.ActionError as e:
            job['outcome'] = 'switch-failed'
            raise jobs.ActionError(f'approved, but the switch failed: {e}') from e
        job['outcome'] = 'switched'
        return done
    finally:
        for name in ('result', 'request.json'):
            (work / name).unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass


# --- the agent's side --------------------------------------------------------------------

def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print(USAGE, end='', file=sys.stdout if argv else sys.stderr)
        return 0 if argv else 64
    if argv[0].startswith('-') or not argv[0].strip():
        print(USAGE, end='', file=sys.stderr)
        return 64
    args = {'reason': argv[0], 'args': argv[1:], 'from': os.getcwd()}
    try:
        jobs.validate('ask-rebuild', args)
    except jobs.ActionError as e:
        print(f'update-manager: {e}', file=sys.stderr)
        return 64
    config.RUNTIME.mkdir(parents=True, exist_ok=True)
    with open(config.RUNTIME / 'ask.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print('update-manager: another request is being started; waiting', file=sys.stderr)
            fcntl.flock(lock, fcntl.LOCK_EX)
        said = False
        while jobs.busy_switching():
            if not said:
                print('update-manager: a switch, rebuild or another request is running; waiting for it',
                      file=sys.stderr)
                said = True
            time.sleep(3)
        try:
            job = jobs.start('ask-rebuild', args, by='ask')
        except (jobs.ActionError, CommandError) as e:
            print(f'update-manager: {e}', file=sys.stderr)
            return 1
    print(f"update-manager: asking the user (job {job['id']}); this waits for their answer", file=sys.stderr)
    job = follow(job['id'])
    outcome = job.get('outcome')
    print(f"update-manager: {job.get('message') or job.get('state')}", file=sys.stderr)
    answer = job.get('answer') or {}
    if answer.get('message'):
        print(f"The user's message: {answer['message']}")
    if job.get('state') == 'done':
        return 0
    return EXIT.get(outcome, 1)


def follow(ident, poll=0.5):
    """Print the job's log as it grows; the job record once it has ended."""
    log = jobs.job_path(ident).with_suffix('.log')
    offset = 0
    while True:
        job = jobs.load(ident) or {}
        offset = copy_new(log, offset)
        state = job.get('state')
        if state not in ('queued', 'running', None):
            copy_new(log, offset)
            return job
        if state == 'running' and time.time() - job.get('saved', time.time()) > 120 and not jobs.unit_alive(job):
            job.update(state='lost', message='the job stopped without saying how it ended')
            return job
        time.sleep(poll)


def copy_new(path, offset):
    try:
        with open(path, 'rb') as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return offset
    if data:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
    return offset + len(data)
