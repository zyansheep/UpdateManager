"""update-manager's commands."""
import json
import sys

from . import __version__, config

USAGE = """\
usage: update-manager COMMAND

The widget
  waybar [--once]            Waybar custom module (reads the agent's state.json)
  panel [machines|upstream]  toggle the resident panel; `panel serve` runs it
  agent [--mock-peer HOST]   the resident agent (update-manager-agent.service)
  status [--json]            what the widget shows, as text (or the whole state)
  refresh                    ask the agent to recheck everything now

Actions (here, or on HOST: over ssh, or queued until HOST next checks in)
  do ACTION [JSON]           start ACTION here; prints the job
  request HOST ACTION [JSON] start ACTION on HOST
  actions                    the ACTION menu
  job list | log ID          recent jobs, a job's output

Checks, printed (the agent runs them on its own schedule)
  check forks|patches|profiles|inputs|update
  config [KEY...]            a setting as in effect (defaults, /etc, ~/.config)
  brief FORK                 the agent brief for a conflicting fork

Plumbing
  exchange --from HOST       peer side of the agent's ssh round trip (state on stdin)
  queue HOST ACTION [JSON]   leave ACTION for HOST in this machine's outbox
  job run ID                 a job's process (systemd-run starts it)
  nixlog --progress FILE [--history FILE]   follow a Nix internal-json log on stdin
  switch-record ...          the root switch helper's bookkeeping
"""


def emit(value):
    print(json.dumps(value, indent=None, default=str))


def parse_args(argv, start):
    if len(argv) > start:
        try:
            return json.loads(argv[start])
        except ValueError:
            raise SystemExit(f'update-manager: arguments must be JSON, not {argv[start]!r}') from None
    return {}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE, end='')
        return 0
    command, rest = argv[0], argv[1:]
    if command in ('-V', '--version', 'version'):
        print(__version__)
        return 0
    if command == 'waybar':
        from . import waybar
        return waybar.main(rest)
    if command == 'panel':
        from . import launch
        return launch.main(rest)
    if command == 'agent':
        from . import agent
        return agent.main(rest)
    if command == 'nixlog':
        from . import nixlog
        return nixlog.main(rest)
    if command == 'status':
        from . import collect, text
        state = collect.read_view()
        if '--json' in rest:
            emit(state)
        else:
            print(text.summary(state))
        return 0
    if command == 'refresh':
        config.RUNTIME.mkdir(parents=True, exist_ok=True)
        (config.RUNTIME / 'refresh').touch()
        print('asked the agent to refresh')
        return 0
    if command == 'exchange':
        from . import collect, remote
        sender = rest[rest.index('--from') + 1] if '--from' in rest else None
        incoming = sys.stdin.read() if sender else ''
        emit(remote.serve_exchange(sender, incoming, collect.published))
        return 0
    if command in ('do', 'request', 'queue'):
        from . import jobs, remote
        try:
            if command == 'do':
                job = jobs.start(rest[0], parse_args(rest, 1), by='cli')
                emit({'ok': True, 'job': job, 'message': f"started: {job['title']}"})
            elif command == 'request':
                result = remote.request(rest[0], rest[1], parse_args(rest, 2))
                emit(result)
                return 0 if result.get('ok') else 1
            else:
                queued = remote.queue(rest[0], rest[1], parse_args(rest, 2))
                emit({'ok': True, 'queued': queued, 'message': f'queued for {rest[0]}'})
        except (jobs.ActionError, IndexError) as e:
            emit({'ok': False, 'message': str(e) or USAGE})
            return 1
        return 0
    if command == 'actions':
        from . import jobs
        print('\n'.join(sorted(jobs.ACTIONS)))
        return 0
    if command == 'job':
        from . import jobs
        if rest[:1] == ['run'] and len(rest) == 2:
            return jobs.run_job(rest[1])
        if rest[:1] == ['log'] and len(rest) == 2:
            print(jobs.job_path(rest[1]).with_suffix('.log').read_text(errors='replace'), end='')
            return 0
        for job in jobs.recent(20):
            print(f"{job['id']:<36} {job['state']:<8} {job['title']}  {job.get('message') or ''}")
        return 0
    if command == 'check':
        return check(rest)
    if command == 'config':
        value = config.get(*rest) if rest else config.load()
        if value is None:
            return 1
        print(value if isinstance(value, str) else json.dumps(value, default=str))
        return 0
    if command == 'switch-record':
        return switch_record(rest)
    if command == 'brief':
        from . import forks
        fork = forks.find(' '.join(rest))
        print(forks.brief(fork, forks.check(fork, do_fetch=False)))
        return 0
    print(USAGE, end='', file=sys.stderr)
    return 2


def check(rest):
    what = rest[0] if rest else 'update'
    if what == 'forks':
        from . import forks
        result = forks.check_all(do_fetch='--no-fetch' not in rest)
        from .util import write_json
        write_json(config.CACHE / 'forks.json', result)
        emit(result)
    elif what == 'patches':
        from . import forks
        emit([forks.patch_check(p) for p in config.get('patches') or []])
    elif what == 'profiles':
        from . import profiles
        emit(profiles.check(estimate='--no-estimate' not in rest))
    elif what == 'inputs':
        from . import system
        emit(system.input_freshness(system.flake_inputs(system.lock_path_for())))
    elif what == 'update':
        from . import collect
        emit(collect.own_state(compute=True))
    else:
        print('usage: update-manager check forks|patches|profiles|inputs|update', file=sys.stderr)
        return 2
    return 0


def switch_record(rest):
    """The switch helper's outcome: <hash>.result.json for the job, and history.json for estimates."""
    import time
    from .util import read_json, write_json
    args = dict(zip(rest[::2], rest[1::2], strict=False))
    now = time.time()
    progress = read_json(args.get('--progress'), {}) or {}
    started, fetched = float(args.get('--started', now)), float(args.get('--fetched', now))
    fetch = progress.get('fetch') or {}
    copied = (progress.get('bytes') or {}).get('copied') or fetch.get('download') or 0
    ok = args.get('--ok') == 'true'
    result = {'ok': ok, 'mode': args.get('--mode'), 'path': args.get('--path'), 'from': args.get('--from'),
              'message': args.get('--message'), 'started': started, 'finished': now}
    write_json(args['--result'], result)
    history = read_json(args['--history'], []) or []
    history.append({'time': now, 'ok': ok, 'mode': args.get('--mode'), 'path': args.get('--path'),
                    'bytes': copied, 'paths': fetch.get('paths'), 'download_s': round(fetched - started, 1),
                    'activation_s': round(now - fetched, 1)})
    write_json(args['--history'], history[-50:])
    return 0
