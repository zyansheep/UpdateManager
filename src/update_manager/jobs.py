"""Actions, and the jobs that run them.

An action is one of a fixed menu (ACTIONS): a remote machine can ask for one by
name with validated arguments, never for an arbitrary command. Each runs as a
job: a transient user unit (`systemd-run --user`, so it outlives the panel and
the ssh session that asked for it) running `update-manager job run ID`, which
records its state, output and progress in ~/.local/state/update-manager/jobs.

Root work is the root units' (nix/module.nix): switching starts
update-manager-switch@<store path>.service (or -boot@) through polkit;
"rebuild" uses the existing passwordless `sudo rebuild`.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import config
from .nixlog import Progress
from .util import CommandError, read_json, run, store_hash, store_name, write_json

JOBS = config.STATE / 'jobs'
STORE_PATH = re.compile(r'^/nix/store/[0-9a-df-np-sv-z]{32}-[A-Za-z0-9+._?=-]+$')
KEEP = 40


class ActionError(ValueError):
    pass


def new_id(kind):
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{os.getpid() % 1000:03d}"


def job_path(ident):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', ident):
        raise ActionError(f'bad job id {ident!r}')
    return JOBS / f'{ident}.json'


def load(ident):
    return read_json(job_path(ident))


def save(job):
    job['saved'] = time.time()
    write_json(job_path(job['id']), job)


def recent(limit=12, now=None):
    """The newest jobs, newest first; a 'running' job whose unit is gone becomes 'lost'."""
    now = now or time.time()
    try:
        files = sorted(JOBS.glob('*.json'), key=lambda p: p.name, reverse=True)
    except OSError:
        return []
    out = []
    for path in files[:limit]:
        job = read_json(path)
        if not job:
            continue
        if job.get('state') == 'running' and now - job.get('saved', now) > 120 and not unit_alive(job):
            job['state'], job['message'] = 'lost', 'the job stopped without saying how it ended'
        out.append({k: job.get(k) for k in ('id', 'kind', 'title', 'state', 'created', 'started', 'finished',
                                             'message', 'progress', 'tail', 'args', 'unit', 'host')})
    for path in files[KEEP:]:
        try:
            path.unlink()
            path.with_suffix('.log').unlink(missing_ok=True)
        except OSError:
            pass
    return out


def unit_alive(job):
    unit = job.get('unit')
    if not unit or not shutil.which('systemctl'):
        return False
    done = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit])
    return done.returncode == 0


def busy(kind=None):
    return [j for j in recent(20) if j['state'] in ('queued', 'running') and (kind is None or j['kind'] == kind)]


# --- the menu ----------------------------------------------------------------------

def validate(action, args):
    """(kind, title, args) for a request, or ActionError. Checks what can be checked anywhere;
    the action itself checks the rest (that a path is this host's CI build, ...)."""
    args = dict(args or {})
    if action not in ACTIONS:
        raise ActionError(f'unknown action {action!r}')
    if action in ('switch', 'boot'):
        path = args.get('path')
        if not path or not STORE_PATH.match(path) or '-nixos-system-' not in path:
            raise ActionError('switch needs the store path of a NixOS system')
        return action, f"{'Switch' if action == 'switch' else 'Boot into'} {store_name(path)[13:] or path}", args
    if action == 'rebuild':
        return action, 'Rebuild from the checkout' + (' (pull first)' if args.get('pull') else ''), \
            {'pull': bool(args.get('pull'))}
    if action in ('ci-run', 'ci-update', 'ci-stop'):
        return action, {'ci-run': 'Build origin/main on CI', 'ci-update': 'Build with updated inputs on CI',
                        'ci-stop': 'Stop the CI build'}[action], {}
    if action == 'ci-apply-lock':
        return action, "Commit CI's updated flake.lock to the checkout", {}
    if action == 'profile-upgrade':
        name = args.get('name')
        if not name or not re.fullmatch(r'[A-Za-z0-9._+-]+|--all', name):
            raise ActionError('profile-upgrade needs a profile element name')
        return action, f"Upgrade {'every profile package' if name == '--all' else name}", {'name': name}
    if action in ('fork-check', 'fork-apply', 'fork-push', 'fork-agent', 'patch-agent', 'patch-check'):
        name = args.get('name')
        if action not in ('fork-check', 'patch-check') and not name:
            raise ActionError(f'{action} needs a name')
        how = args.get('how')
        if how not in (None, 'fast-forward', 'merge', 'rebase'):
            raise ActionError(f'bad merge kind {how!r}')
        verb = {'fork-check': 'Check upstreams', 'fork-apply': 'Update', 'fork-push': 'Push',
                'fork-agent': 'Hand to an agent:', 'patch-agent': 'Hand to an agent: patches of',
                'patch-check': 'Check patched packages'}[action]
        return action, f'{verb} {name or "all"}'.strip(), {k: v for k, v in (('name', name), ('how', how)) if v}
    if action == 'refresh':
        return action, 'Refresh', {}
    raise ActionError(action)


def do_switch(job, args, log):
    """Start the root unit for this system and follow its progress file until it ends."""
    path, mode = args['path'], job['kind']
    host = config.hostname()
    if f'-nixos-system-{host}-' not in path:
        raise ActionError(f'{store_name(path)} is not a system for {host}')
    unit = f"update-manager-{mode}@{escape_instance(os.path.basename(path))}.service"
    if not unit_exists(unit):
        raise ActionError('the update-manager root units are not installed here yet '
                          '(services.update-manager in nixos-conf); "Rebuild" works without them')
    job['root_unit'] = unit
    save(job)
    progress_file = config.SYSTEM_RUNTIME / f'{store_hash(path)}.json'
    proc = subprocess.Popen(['systemctl', 'start', unit], stdout=log, stderr=log)
    while proc.poll() is None:
        time.sleep(1)
        snap = read_json(progress_file)
        if snap:
            job['progress'] = snap
            save(job)
    result = read_json(config.SYSTEM_RUNTIME / f'{store_hash(path)}.result.json') or {}
    if proc.returncode != 0 or not result.get('ok', proc.returncode == 0):
        tail = journal_tail(unit)
        log.write(tail.encode())
        raise ActionError(result.get('message') or f'{unit} failed: ' + (tail.strip().splitlines() or ['?'])[-1])
    return result.get('message') or f'switched to {store_name(path)}'


def escape_instance(name):
    return run(['systemd-escape', name], timeout=5).strip()


def unit_exists(unit):
    done = subprocess.run(['systemctl', 'cat', unit], capture_output=True)
    return done.returncode == 0


def journal_tail(unit, lines=15):
    try:
        return run(['journalctl', '--no-pager', '-o', 'cat', '-n', str(lines), '-u', unit], timeout=10)
    except CommandError:
        return ''


def do_rebuild(job, args, log):
    repo = config.flake_dir()
    if args.get('pull'):
        status = run(['git', '-C', repo, 'status', '--porcelain', '--untracked-files=no'], timeout=20)
        if status.strip():
            raise ActionError(f'{repo} has uncommitted changes: not pulling')
        log.write(run(['git', '-C', repo, 'pull', '--ff-only'], timeout=120).encode())
    rebuild = '/run/current-system/sw/bin/rebuild'
    if not os.path.exists(rebuild):
        raise ActionError('no `rebuild` here (core.rebuild)')
    # Plain text: nixos-rebuild's own flags vary between versions; Progress reads the plan lines.
    return stream(job, ['/run/wrappers/bin/sudo', '-n', rebuild], log, done='rebuilt and switched')


def stream(job, argv, log, done='done', cwd=None):
    """Run argv, feeding Nix's internal-json stderr through Progress into the job record."""
    progress = Progress()
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.PIPE, text=True, cwd=cwd)
    last = 0.0
    for line in proc.stderr:
        text = progress.feed(line)
        if text:
            log.write((text + '\n').encode())
        if time.time() - last > 1:
            job['progress'] = progress.snapshot(read_json(config.SYSTEM_STATE / 'build-times.json', {}))
            job['tail'] = progress.last_line[:200]
            save(job)
            last = time.time()
    proc.wait()
    job['progress'] = progress.snapshot()
    if proc.returncode != 0:
        raise ActionError((progress.errors or [f'exit {proc.returncode}'])[-1])
    return done


def do_ci(job, args, log):
    cfg = config.get('nixos_ci')
    if not Path(cfg['state']).is_dir():
        raise ActionError(f"no nixos-ci on {config.hostname()}")
    if job['kind'] == 'ci-stop':
        run(['systemctl', 'stop', *cfg['units']], timeout=60)
        return 'stopped'
    unit = 'nixos-ci-update.service' if job['kind'] == 'ci-update' else 'nixos-ci.service'
    run(['systemctl', 'start', '--no-block', unit], timeout=30)
    return f'started {unit}'


def do_apply_lock(job, args, log):
    cfg = config.get('nixos_ci')
    lock = Path(cfg['state']) / 'update.flake.lock'
    status = (Path(cfg['state']) / 'update.status')
    if not lock.exists() or 'lock' not in status.read_text():
        raise ActionError('no updated flake.lock that built for every host')
    repo = config.flake_dir()
    dirty = run(['git', '-C', repo, 'status', '--porcelain', '--', 'flake.lock'], timeout=20)
    if dirty.strip():
        raise ActionError('flake.lock has uncommitted changes in the checkout')
    (repo / 'flake.lock').write_bytes(lock.read_bytes())
    run(['git', '-C', repo, 'commit', '-m', 'flake.lock: update (built by nixos-ci)', '--', 'flake.lock'], timeout=30)
    return f'committed the updated flake.lock in {repo} (not pushed)'


def do_profile(job, args, log):
    name = args['name']
    argv = ['nix', 'profile', 'upgrade', '--log-format', 'internal-json']
    argv += ['--all'] if name == '--all' else [name]
    return stream(job, argv, log, done=f'upgraded {name}')


def do_fork(job, args, log):
    from . import forks, handoff
    kind = job['kind']
    if kind == 'fork-check':
        items = [forks.find(args['name'])] if args.get('name') else None
        result = forks.check_all(items)
        write_json(config.CACHE / 'forks.json', merge_forks(read_json(config.CACHE / 'forks.json', {}), result))
        bad = [i['name'] for i in result['items'] if i.get('status') == 'error']
        return 'checked' + (f" ({', '.join(bad)} failed)" if bad else '')
    if kind == 'patch-check':
        patches = [p for p in config.get('patches') or [] if not args.get('name') or p['name'] == args['name']]
        result = {'checked': time.time(), 'items': [forks.patch_check(p) for p in patches]}
        write_json(config.CACHE / 'patches.json', result)
        return 'checked'
    if kind == 'patch-agent':
        state = next((p for p in (read_json(config.CACHE / 'patches.json', {}) or {}).get('items', [])
                      if p['name'] == args['name']), None)
        if not state:
            raise ActionError(f"no check result for {args['name']} yet")
        result = handoff.launch(f"patches-{forks.safe_name(args['name'])}", config.get('flake'),
                                config.get('flake_branch'), forks.patch_brief(state),
                                f"Rebase nixos-conf's {args['name']} patches onto {state.get('upstream')}")
        job['handoff'] = result
        return result['message']
    fork = forks.find(args['name'])
    if kind == 'fork-apply':
        result = forks.apply(fork, args.get('how'))
        if not result['ok']:
            raise ActionError(result['message'])
        refresh_fork(fork)
        return result['message']
    if kind == 'fork-push':
        result = forks.push(fork)
        if not result['ok']:
            raise ActionError(result['message'])
        return result['message']
    if kind == 'fork-agent':
        state = forks.check(fork, do_fetch=False)
        brief = forks.brief(fork, state)
        job['brief'] = brief
        result = handoff.launch(forks.safe_name(fork['name']), fork['repo'], state.get('branch') or 'HEAD', brief,
                                f"Port {state.get('upstream')} into {fork['name']}")
        job['handoff'] = result
        return result['message']
    raise ActionError(kind)


def merge_forks(old, new):
    items = {i['name']: i for i in (old or {}).get('items', [])}
    for item in new.get('items', []):
        items[item['name']] = item
    order = [f['name'] for f in config.get('forks') or []]
    return {'checked': new.get('checked'), 'items': sorted(items.values(), key=lambda i: order.index(i['name'])
                                                           if i['name'] in order else 99)}


def refresh_fork(fork):
    from . import forks
    write_json(config.CACHE / 'forks.json',
               merge_forks(read_json(config.CACHE / 'forks.json', {}), {'items': [forks.check(fork, False)]}))


def do_refresh(job, args, log):
    (config.RUNTIME / 'refresh').parent.mkdir(parents=True, exist_ok=True)
    (config.RUNTIME / 'refresh').touch()
    return 'asked the agent to refresh'


ACTIONS = {
    'switch': do_switch, 'boot': do_switch, 'rebuild': do_rebuild,
    'ci-run': do_ci, 'ci-update': do_ci, 'ci-stop': do_ci, 'ci-apply-lock': do_apply_lock,
    'profile-upgrade': do_profile,
    'fork-check': do_fork, 'fork-apply': do_fork, 'fork-push': do_fork, 'fork-agent': do_fork,
    'patch-agent': do_fork, 'patch-check': do_fork,
    'refresh': do_refresh,
}


# --- starting and running ------------------------------------------------------------

def start(action, args=None, by=None, wait=False):
    """Validate and start a job here. Returns the job record."""
    kind, title, args = validate(action, args)
    if kind in ('switch', 'boot', 'rebuild') and busy('switch') + busy('boot') + busy('rebuild'):
        raise ActionError('a switch or rebuild is already running')
    job = {'id': new_id(kind), 'kind': kind, 'title': title, 'args': args, 'state': 'queued',
           'created': time.time(), 'host': config.hostname(), 'by': by}
    JOBS.mkdir(parents=True, exist_ok=True)
    unit = f"update-manager-job-{job['id']}"
    job['unit'] = f'{unit}.service'
    save(job)
    command = [*config.self_command(), 'job', 'run', job['id']]
    if wait or not shutil.which('systemd-run') or os.environ.get('UPDATE_MANAGER_NO_SYSTEMD_RUN'):
        if wait:
            run_job(job['id'])
            return load(job['id'])
        job['unit'] = None
        save(job)
        subprocess.Popen(command, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return job
    env = [f'--setenv={k}={os.environ[k]}' for k in ('PATH', 'WAYLAND_DISPLAY', 'UPDATE_MANAGER_HOST',
                                                     'UPDATE_MANAGER_STATE_DIR', 'UPDATE_MANAGER_CACHE_DIR',
                                                     'UPDATE_MANAGER_RUNTIME_DIR', 'XDG_CONFIG_HOME')
           if k in os.environ]
    run(['systemd-run', '--user', '--quiet', '--collect', f'--unit={unit}', f'--description={title}',
         '--property=TimeoutStopSec=30', *env, *command], timeout=30)
    return job


def run_job(ident):
    """The job's process: run the action, record how it ended."""
    job = load(ident)
    if not job:
        print(f'update-manager: no job {ident}', file=sys.stderr)
        return 1
    job['state'], job['started'] = 'running', time.time()
    save(job)
    log_path = job_path(ident).with_suffix('.log')
    heartbeat = threading.Event()

    def beat():
        while not heartbeat.wait(30):
            current = load(ident) or job
            if current.get('state') == 'running':
                save(current)
    threading.Thread(target=beat, daemon=True).start()
    with open(log_path, 'ab') as log:
        try:
            message = ACTIONS[job['kind']](job, job.get('args') or {}, log)
            job.update(state='done', message=message)
        except (ActionError, CommandError, KeyError, OSError) as e:
            job.update(state='failed', message=str(e))
        except Exception as e:  # noqa: BLE001
            job.update(state='failed', message=f'{type(e).__name__}: {e}')
            log.write(traceback.format_exc().encode())
    heartbeat.set()
    job['finished'] = time.time()
    save(job)
    notify(job)
    return 0 if job['state'] == 'done' else 1


def notify(job):
    if job['kind'] == 'refresh' or not os.environ.get('WAYLAND_DISPLAY') or not shutil.which('notify-send'):
        return
    subprocess.run(['notify-send', '--app-name=Updates', f"{job['title']}: {job['state']}",
                    job.get('message') or ''], capture_output=True)


def describe(job):
    return json.dumps({k: job.get(k) for k in ('id', 'kind', 'title', 'state', 'message', 'host')})


def shell(args):
    return ' '.join(shlex.quote(str(a)) for a in args)
