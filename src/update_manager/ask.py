"""Asking the user to approve a rebuild: `update-manager ask-rebuild REASON [REBUILD ARGS...]`.

For coding agents, which may not switch the system on their own (and, started with NoNewPrivileges,
can't sudo). The command starts an ask-rebuild job: a transient user unit like any job, so it runs
outside the agent's restrictions. The job stages the checkout (`git add .`, as `nrb` does), builds
the system without root (`ask.preview`, nixos-conf's `rebuild --build-only`) and asks the user in two
places at once:

- a full-screen window (approve.py), when there is a graphical session: the agent's reason, the
  closure diff, the commits since the running system and the uncommitted files, a message box,
  Approve and Deny;
- dither-chat's approvals (`dither-ask`, docs/approvals.md in dither-chat), when it is set up: a
  `rebuild` request with the same text, answered on the phone or the page. Only an approve signed by
  a device in the keys file (`ask.dither_keys`), over this request's payload (host, running commit,
  the diff's hash, the built system) and text, counts.

The first answer wins: a window answer cancels the request, an answer on a device closes the window.
On Approve it runs `ask.switch` (`sudo -n rebuild`) with the agent's rebuild args. If the checkout
changed while the user was asked, it builds and asks again, so what gets switched is what was shown.

The command follows the job's log and exits with EXIT[outcome]; the user's message is printed last.
"""
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from . import config, jobs, system, text
from .util import CommandError, expand, run, write_json

EXIT = {'switched': 0, 'current': 0, 'denied': 77, 'timeout': 75, 'no-window': 69}
USAGE = """\
usage: update-manager ask-rebuild REASON [REBUILD ARGS...]

Asks the user to switch this machine to the checkout (`git add .`, then `sudo rebuild REBUILD ARGS`):
in a full-screen window when there is a graphical session, and on their phone through dither-chat's
approvals when that is set up (DITHER_ASK_URL, DITHER_ASK_TOKEN_FILE); the first answer wins. The
user sees REASON, what changes, and can send a message back. Blocks until the user answers and the
switch ends; if this command is interrupted the job goes on (`update-manager job list`,
`update-manager job log ID`).
Exit: 0 switched (or nothing to switch), 77 denied, 75 no (verified) answer in time, 69 nowhere to
ask (no window could be shown and dither-chat can't be reached), 1 the build or the switch failed.
"""
DITHER_KEYS = '/etc/dither-approvals/keys'
DITHER_ID = re.compile(r'apv_[A-Za-z0-9_-]{22}')
BODY_MAX = 32 * 1024  # dither-chat's limit, in UTF-8 bytes
TITLE_MAX = 200  # in UTF-16 code units (JavaScript's length)
RETRY_S = 15  # after the hub couldn't be reached while waiting


def command(key):
    value = config.get('ask', key)
    return [str(expand(str(a))) for a in (shlex.split(value) if isinstance(value, str) else value or [])]


def say(log, text):
    log.write(f'ask-rebuild: {text}\n'.encode())
    log.flush()


def stage(repo):
    """`git add .` (flakes see only what git tracks); the staged tree, to tell whether it changed."""
    run(['git', '-C', repo, 'add', '.'], timeout=60)
    return run(['git', '-C', repo, 'write-tree'], timeout=30).strip()


def request(job, args, repo, current, new, note=None):
    """What the window shows (and what the dither request's text is made from)."""
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


# --- the window --------------------------------------------------------------------------

def graphical():
    """Whether a window could be shown: a Wayland socket that exists, or an X display."""
    wayland = os.environ.get('WAYLAND_DISPLAY')
    if wayland:
        socket = wayland if os.path.isabs(wayland) else os.path.join(
            os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'), wayland)
        if os.path.exists(socket):
            return True
    return bool(os.environ.get('DISPLAY'))


def open_window(path, work):
    """The window's process, or None when there is no graphical session to show it in."""
    window = os.environ.get('UPDATE_MANAGER_ASK_WINDOW')
    if not window and not graphical():
        return None
    argv = shlex.split(window) if window else [*config.self_command(), 'approve-window']
    with open(work / 'window.out', 'wb') as out, open(work / 'window.err', 'wb') as err:
        try:
            return subprocess.Popen([*argv, str(path)], stdout=out, stderr=err, stdin=subprocess.DEVNULL)
        except OSError as e:
            raise jobs.ActionError(f'could not show the window: {e}') from e


def window_answer(proc, work):
    """The ended window's answer: {'decision': 'approve'|'deny'|'timeout', 'message'}, or ActionError."""
    stdout = (work / 'window.out').read_text(errors='replace')
    stderr = (work / 'window.err').read_text(errors='replace')
    answer = None
    for line in reversed(stdout.splitlines()):
        try:
            answer = json.loads(line)
            break
        except ValueError:
            continue
    if proc.returncode != 0 or not isinstance(answer, dict) or answer.get('decision') not in (
            'approve', 'deny', 'timeout'):
        tail = ' / '.join(stderr.strip().splitlines()[-3:])
        raise jobs.ActionError(f'could not show the window (exit {proc.returncode}){": " + tail if tail else ""}')
    return {'decision': answer['decision'], 'message': str(answer.get('message') or '')[:4000], 'via': 'window'}


def close(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# --- dither-chat's approvals -------------------------------------------------------------

def dither_setup(log):
    """How to ask through dither-chat (`dither-ask`), or None when it is off or not set up (said why)."""
    mode = config.get('ask', 'dither')
    mode = 'on' if mode is True else 'off' if mode is False else str(mode or 'auto').lower()
    if mode in ('off', 'false', 'no'):
        return None
    argv = command('dither_command') or ['dither-ask']
    url = config.get('ask', 'dither_url') or os.environ.get('DITHER_ASK_URL')
    token = config.get('ask', 'dither_token_file') or os.environ.get('DITHER_ASK_TOKEN_FILE')
    keys = str(expand(config.get('ask', 'dither_keys') or os.environ.get('DITHER_ASK_KEYS') or DITHER_KEYS))
    missing = [why for ok, why in (
        (shutil.which(argv[0]), f'no {argv[0]}'),
        (url, 'no hub (DITHER_ASK_URL)'),
        (token, 'no token file (DITHER_ASK_TOKEN_FILE)'),
        (os.path.isfile(keys), f'no keys file {keys} (DITHER_ASK_KEYS): no approval could verify'))
        if not ok]
    if missing:
        if mode == 'on':
            say(log, f"!!! ask.dither is on, but not asking on your devices: {'; '.join(missing)}")
        else:
            say(log, f"not asking on your devices: {'; '.join(missing)}")
        return None
    return {'argv': argv, 'hub': ['--url', url, '--token-file', str(expand(token))], 'keys': keys}


def dither(d, *args, timeout=60):
    """(exit code, stdout, stderr) of `dither-ask ARGS`; 69 when it couldn't run."""
    try:
        done = subprocess.run([*d['argv'], *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 69, '', f'dither-ask {args[0]} timed out after {timeout} s'
    except OSError as e:
        return 69, '', str(e)
    return done.returncode, done.stdout, done.stderr


def json_object(stdout):
    try:
        value = json.loads(stdout)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def tail(stderr):
    return ' / '.join(stderr.strip().splitlines()[-2:])


def sha256(data):
    return hashlib.sha256(data.encode()).hexdigest()


def clean_title(title):
    """A title as dither-chat keeps and hashes it (cleanTitle): one line, at most 200 characters."""
    title = re.sub(r'[\x00-\x1f\x7f]', ' ', title).strip().strip('﻿')
    if len(title.encode('utf-16-le')) // 2 <= TITLE_MAX:
        return title
    while len(title.encode('utf-16-le')) // 2 > TITLE_MAX - 1:
        title = title[:-1]
    return title.rstrip() + '…'


def running_commit(repo, running):
    """(the 40-hex commit the payload names as running, and a note for the body when it isn't the
    running system's own configurationRevision)."""
    if running and not running.endswith('-dirty'):
        done = run(['git', '-C', repo, 'rev-parse', '--verify', '--quiet', f'{running}^{{commit}}'], timeout=15,
                   check=False)
        if done.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}', done.stdout.strip()):
            return done.stdout.strip(), None
    head = run(['git', '-C', repo, 'rev-parse', 'HEAD'], timeout=15).strip()
    if not running:
        why = "The running system's commit is unknown"
    elif running.endswith('-dirty'):
        why = f'The running system was built from {running.removesuffix("-dirty")[:12]} with uncommitted changes'
    else:
        why = f"The running system's commit {running[:12]} is not in this checkout"
    return head, f"{why}: 'running' below is the checkout's HEAD {head[:12]}, and the diff's hash is against it."


def body_text(r, running_note):
    """What the window shows, as text: the device signs over the title and this."""
    lines = [f"An agent asks to switch {r['host']} to a new build" + (f" (from {r['from']})" if r.get('from') else '')
             + ':', '', r['reason'], '']
    if r.get('note'):
        lines += [r['note'], '']
    if r.get('args'):
        lines += [f"Rebuild args: {shlex.join(r['args'])}", '']
    lines.append('WHAT CHANGES')
    if r.get('diff_error'):
        lines.append(f"Could not compare the closures: {r['diff_error']}")
    diff = r.get('diff')
    if diff is not None:
        lines.append(text.diff_summary(diff))
        lines += [f"  {c['name']}  {text.change_text(c)}" for c in diff.get('changes', [])]
        if diff['count'] > len(diff['changes']):
            lines.append(f"  and {diff['count'] - len(diff['changes'])} more")
    lines += ['', 'SOURCE']
    if running_note:
        lines.append(running_note)
    commits = r.get('commits')
    if commits is None:
        lines.append("The commits since the running system are unknown.")
    elif not commits:
        lines.append('No new commits since the running system.')
    else:
        lines.append(f"{len(commits)} commit{'s' if len(commits) != 1 else ''} since the running system:")
        lines += [f"  {c['rev'][:8]} {c['subject']}" for c in commits]
    files = r.get('uncommitted') or []
    if files:
        lines.append(f"{len(files)} uncommitted file{'s' if len(files) != 1 else ''}:")
        lines += [f"  {f.replace(chr(9), ' ')}" for f in files]
    body = '\n'.join(lines).replace('\r\n', '\n') + '\n'
    data = body.encode()
    if len(data) <= BODY_MAX:
        return body
    cut = data[:BODY_MAX - 100].decode(errors='ignore').rsplit('\n', 1)[0]
    return f'{cut}\n… (cut to fit 32 KiB; the rest is in the window and the job log)\n'


def dither_request(d, r, repo, work, log):
    """Ask on the user's devices. {'id', 'title', 'body', 'payload'}, or None (said why)."""
    running, running_note = running_commit(repo, r.get('running_rev'))
    code, out, err = dither(d, 'diff-hash', str(repo), '--base', running)
    diff_hash = out.strip()
    if code != 0 or not re.fullmatch(r'[0-9a-f]{64}', diff_hash):
        say(log, f'!!! dither-ask diff-hash failed (exit {code}): {tail(err)}; not asking on your devices')
        return None
    asked = {'title': clean_title(f"Switch {r['host']} to a new build? {r['reason'].splitlines()[0]}"),
             'body': body_text(r, running_note),
             'payload': ''.join(f'{line}\n' for line in (
                 'dither-rebuild-v1', f"host {r['host']}", f'running {running}', f'diff-sha256 {diff_hash}',
                 f"system {r['new']}"))}
    (work / 'dither-body').write_text(asked['body'])
    (work / 'dither-payload').write_text(asked['payload'])
    code, out, err = dither(d, 'request', '--kind', 'rebuild', '--title', asked['title'],
                            '--body-file', str(work / 'dither-body'), '--payload-file', str(work / 'dither-payload'),
                            '--expires', str(int(r['timeout'])), *d['hub'])
    answer = json_object(out)
    if not DITHER_ID.fullmatch(str(answer.get('id') or '')) or answer.get('state') != 'waiting':
        why = 'the hub cannot be reached or refused the token' if code == 69 else f'exit {code}'
        say(log, f"dither-chat: could not ask ({why}){': ' + tail(err) if tail(err) else ''}")
        return None
    asked['id'] = answer['id']
    say(log, f"asked on your devices too ({asked['id']})")
    return asked


def statement_fields(statement):
    """The signed statement's `key value` lines (dither-approval-v1), or {}."""
    lines = (statement or '').split('\n')
    if not statement or not statement.endswith('\n') or lines[0] != 'dither-approval-v1':
        return {}
    return dict(line.split(' ', 1) for line in lines[1:-1] if ' ' in line)


def approval_problem(answer, asked):
    """Why a dither-ask 'approve' doesn't count, or None. dither-ask checked the signature against the
    keys file; this checks it was over this request: its id, payload and text, the action and message."""
    if answer.get('state') != 'answered' or answer.get('action') != 'approve':
        return f"it answered {answer.get('action') or answer.get('state')}"
    if answer.get('verified') is not True or not answer.get('signed'):
        return 'dither-ask did not verify a signature'
    s = statement_fields(answer.get('statement'))
    expect = {'id': asked['id'], 'kind': 'rebuild', 'action': 'approve',
              'payload-sha256': sha256(asked['payload']),
              'display-sha256': sha256(f"{asked['title']}\n\n{asked['body']}"),
              'message-sha256': sha256(answer.get('message') or '')}
    wrong = [k for k, v in expect.items() if s.get(k) != v]
    return f"the signed statement's {', '.join(wrong)} {'is' if len(wrong) == 1 else 'are'} not this request's" \
        if wrong else None


def device_name(answer):
    device = answer.get('device') or {}
    return device.get('name') or device.get('id') or 'a device'


def wait_status(d, asked, deadline, work):
    """`dither-ask status ID --wait ... --keys ...` in the background, until a little after the expiry."""
    left = max(1, int(deadline - time.monotonic()) + 5)
    with open(work / 'dither.out', 'wb') as out, open(work / 'dither.err', 'wb') as err:
        return subprocess.Popen([*d['argv'], 'status', asked['id'], *d['hub'], '--wait', str(left),
                                 '--keys', d['keys']], stdout=out, stderr=err, stdin=subprocess.DEVNULL)


def dither_answer(proc, asked, work, log):
    """What the ended `status` said: an answer, 'again' (still waiting), 'later' (hub unreachable),
    or ('over', why) (expired, cancelled, or an answer that doesn't count)."""
    code = proc.returncode
    answer = json_object((work / 'dither.out').read_text(errors='replace'))
    err = tail((work / 'dither.err').read_text(errors='replace'))
    message = str(answer.get('message') or '')[:4000]
    if answer.get('id') not in (None, asked['id']):
        say(log, f"!!! dither-ask status answered for {answer.get('id')}, not {asked['id']}; ignoring it")
        return 'over', 'dither-ask answered for another request'
    if code == 0:
        problem = approval_problem(answer, asked)
        if problem is None:
            return {'decision': 'approve', 'message': message, 'via': 'dither', 'device': device_name(answer)}
        say(log, f'!!! an approval from {device_name(answer)} does NOT count: {problem}')
        return 'over', f'the approval from {device_name(answer)} did not verify ({problem})'
    if code == 77:
        return {'decision': 'deny', 'message': message, 'via': 'dither', 'device': device_name(answer)}
    if code == 75:
        if answer.get('state') == 'waiting':
            return 'again'
        say(log, f"the request on your devices {answer.get('state') or 'ended'} without an answer")
        return 'over', None
    if code == 76:
        say(log, f'!!! {device_name(answer)} answered {answer.get("action")}, but it does NOT verify against '
                 f"the keys file{': ' + err if err else ''}; not switching on it")
        return 'over', f'the answer from {device_name(answer)} did not verify against the keys file'
    if code == 69:
        say(log, f"dither-chat can't be reached while waiting{': ' + err if err else ''}; trying again")
        return 'later'
    say(log, f"!!! dither-ask status failed (exit {code}){': ' + err if err else ''}")
    return 'over', None


def cancel(d, asked, reason, log):
    code, _out, err = dither(d, 'cancel', asked['id'], '--reason', reason, *d['hub'], timeout=30)
    if code == 0:
        say(log, f"cancelled the request on your devices ({reason})")
    else:
        say(log, f"could not cancel {asked['id']} (exit {code}){': ' + tail(err) if tail(err) else ''}")


# --- asking --------------------------------------------------------------------------------

def ask_user(job, r, path, repo, work, d, log):
    """The first answer from the window or the user's devices: {'decision': 'approve'|'deny'|'timeout',
    'message', 'via', 'device'?, 'why'?}. ActionError when there is nowhere to ask."""
    timeout = int(r['timeout'])
    deadline = time.monotonic() + timeout
    asked = dither_request(d, r, repo, work, log) if d else None
    window = waiter = None
    gone = f'no longer asked on {r["host"]}'  # why the request on the devices is cancelled
    try:
        try:
            window = open_window(path, work)
        except jobs.ActionError as e:
            if not asked:
                raise
            say(log, f'{e}; asking on your devices alone')
        if window is None and not asked:
            raise jobs.ActionError('nowhere to ask: no graphical session for the window, and dither-chat '
                                   + ('is not set up' if not d else "can't be reached"))
        if window is not None and d and not asked:
            say(log, 'asking in the window alone')
        where = ' and '.join(w for w, on in (('full-screen window', window), ('your devices', asked)) if on)
        say(log, f'waiting for the user to answer ({where})')
        job.update(outcome='asking', tail='waiting for your answer', progress=None)
        if asked:
            job['dither_id'] = asked['id']
            waiter = wait_status(d, asked, deadline, work)
        jobs.save(job)
        waiting = bool(asked)  # the request on the devices may still be answered
        retry_at = why = None
        while True:
            now = time.monotonic()
            if window is not None and window.poll() is not None:
                ended, window = window, None
                try:
                    answer = window_answer(ended, work)
                    gone = f'answered in the window on {r["host"]}'
                    return answer
                except jobs.ActionError as e:
                    if not waiting:
                        raise
                    say(log, f'{e}; waiting on your devices alone')
            if waiter is not None and waiter.poll() is not None:
                ended, waiter = waiter, None
                result = dither_answer(ended, asked, work, log)
                if isinstance(result, dict):
                    waiting = False
                    return result
                if result == 'again' and now < deadline:
                    waiter = wait_status(d, asked, deadline, work)
                elif result in ('again', 'later'):
                    retry_at = now + RETRY_S
                else:
                    waiting, why = False, result[1] or why
            if retry_at is not None and now >= retry_at:
                retry_at = None
                if now < deadline:
                    waiter = wait_status(d, asked, deadline, work)
            if window is None and waiter is None and (retry_at is None or now >= deadline):
                return {'decision': 'timeout', 'message': '', 'via': 'dither' if asked else 'window', 'why': why}
            if now > deadline + 60:  # neither said the time is up
                return {'decision': 'timeout', 'message': '', 'via': None, 'why': why}
            time.sleep(0.2)
    finally:
        close(window)
        close(waiter)
        if asked and waiting:
            cancel(d, asked, gone, log)


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
    d = dither_setup(log)
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
            r = request(job, args, repo, current, new, note)
            write_json(path, r)
            try:
                answer = ask_user(job, r, path, repo, work, d, log)
            except jobs.ActionError:
                job['outcome'] = 'no-window'
                raise
            why = answer.pop('why', None)
            job['answer'] = answer
            message = f": {answer['message']}" if answer['message'] else ''
            where = f" on {answer['device']}" if answer.get('device') else ' in the window' \
                if answer.get('via') == 'window' else ''
            if answer['decision'] == 'timeout':
                job['outcome'] = 'timeout'
                raise jobs.ActionError(f'no verified answer: {why}; not switching' if why
                                       else 'no answer in time, not switching')
            if answer['decision'] != 'approve':
                job['outcome'] = 'denied'
                say(log, f'denied{where}{message}')
                raise jobs.ActionError(f'denied{message}')
            if stage(repo) != tree:
                note = 'The checkout changed while you were asked. This is the new build.'
                say(log, 'the checkout changed while you were asked; building again')
                continue
            break
        say(log, f'approved{where}{message}; switching')
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
        for name in ('result', 'request.json', 'window.out', 'window.err', 'dither.out', 'dither.err',
                     'dither-body', 'dither-payload'):
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
