"""Talking to the other machines, over ssh on the tailnet.

One round trip does everything: `ssh functor update-manager exchange --from isomorph`
with isomorph's own state on stdin answers with functor's state and any actions
queued for isomorph. So the laptop needs no sshd: it pushes its state each time
it pulls, and functor shows the last state it got ("seen 3 min ago").

Asking another machine to do something is `ssh HOST update-manager do ACTION
'{json args}'`: the same fixed menu as locally (jobs.ACTIONS). For a machine
that takes no connections the request waits in the CI host's outbox until that
machine next exchanges.
"""
import json
import os
import shlex
import time
import uuid

from . import config, jobs
from .util import CommandError, read_json, run, write_json

PEERS = config.CACHE / 'peers'
OUTBOX = config.STATE / 'outbox'


def ssh_args(host, settings=None):
    entry = (config.get('hosts', settings=settings) or {}).get(host, {})
    dest = entry.get('ssh', host)
    config.RUNTIME.mkdir(parents=True, exist_ok=True)
    control = [
        '-o', 'ControlMaster=auto', '-o', f"ControlPath={config.RUNTIME}/ssh-%C", '-o', 'ControlPersist=10m',
    ]
    return ['ssh', *config.get('remote', 'ssh_options', settings=settings), *control, dest]


def shell_command(args, settings=None):
    """The remote command line: the first update-manager candidate that exists runs `args`."""
    quoted = ' '.join(shlex.quote(a) for a in args)
    parts = []
    for candidate in config.remote_candidates(settings):
        if ' ' in candidate:  # e.g. "nix run /home/me/Projects/UpdateManager --"
            parts.append(f'exec {candidate} {quoted}')
            break
        path = candidate.replace('~', '$HOME', 1) if candidate.startswith('~') else shlex.quote(candidate)
        parts.append(f'if [ -x {path} ]; then exec {path} {quoted}; fi')
    parts.append('echo "update-manager: not installed here" >&2; exit 127')
    return '; '.join(parts)


def call(host, args, stdin=None, timeout=40, settings=None):
    """Run update-manager args on host; parsed JSON output."""
    text = run([*ssh_args(host, settings), shell_command(args, settings)], input=stdin, timeout=timeout)
    return json.loads(text)


def exchange(peer, own_state, settings=None, timeout=40):
    """Push own_state to peer, get the peer's state and the requests waiting for us."""
    return call(peer, ['exchange', '--from', config.hostname()], stdin=json.dumps(own_state, default=str),
                timeout=timeout, settings=settings)


def serve_exchange(sender, incoming_text, own_state_fn):
    """The peer side of exchange: store what the sender sent, answer with our state and its outbox."""
    now = time.time()
    if sender and incoming_text.strip():
        try:
            incoming = json.loads(incoming_text)
            if isinstance(incoming, dict):
                write_json(PEERS / f'{safe(sender)}.json', {'received': now, 'state': incoming})
        except ValueError:
            pass
    reply = {'time': now, 'host': config.hostname(), 'state': own_state_fn(),
             'requests': take_outbox(sender) if sender else []}
    return reply


def safe(name):
    return ''.join(c for c in name if c.isalnum() or c in '-_.') or 'unknown'


def peer_state(name):
    """The last state a peer pushed to us (or we pulled from it): {'received', 'state'}."""
    return read_json(PEERS / f'{safe(name)}.json')


def save_peer(name, state):
    write_json(PEERS / f'{safe(name)}.json', {'received': time.time(), 'state': state})


# --- the outbox: requests for machines that take no connections ------------------------

def queue(host, action, args, by=None):
    jobs.validate(action, args)
    request = {'id': uuid.uuid4().hex[:12], 'action': action, 'args': args or {}, 'queued': time.time(),
               'by': by or config.hostname()}
    path = OUTBOX / f'{safe(host)}.json'
    pending = read_json(path, []) or []
    # One switch at a time: a newer switch request replaces an older one.
    if action in ('switch', 'boot', 'rebuild'):
        pending = [r for r in pending if r['action'] not in ('switch', 'boot', 'rebuild')]
    pending.append(request)
    write_json(path, pending)
    return request


def pending(host):
    return read_json(OUTBOX / f'{safe(host)}.json', []) or []


def take_outbox(host):
    path = OUTBOX / f'{safe(host)}.json'
    items = read_json(path, []) or []
    if items:
        try:
            os.unlink(path)
        except OSError:
            pass
        # Older than a day: the moment for it has passed.
        items = [r for r in items if time.time() - r.get('queued', 0) < 86400]
    return items


# --- asking for an action anywhere -------------------------------------------------------

def request(host, action, args=None, settings=None):
    """Do action on host: here, over ssh, or through the CI host's outbox. Returns
    {'ok', 'via', 'job'|'queued', 'message'}."""
    me = config.hostname()
    hosts = config.get('hosts', settings=settings) or {}
    if host in (None, me):
        job = jobs.start(action, args, by=me)
        return {'ok': True, 'via': 'local', 'job': job, 'message': f"started: {job['title']}"}
    entry = hosts.get(host, {})
    ci_host = config.get('ci_host', settings=settings)
    if entry.get('pull', True):
        try:
            reply = call(host, ['do', action, json.dumps(args or {})], timeout=60, settings=settings)
            return dict(reply, via='ssh')
        except (CommandError, ValueError) as e:
            if me != ci_host:
                return {'ok': False, 'via': 'ssh', 'message': f'{host}: {e}'}
    if me == ci_host:
        queued = queue(host, action, args, by=me)
        return {'ok': True, 'via': 'outbox', 'queued': queued,
                'message': f"queued for {host}: it runs when {host} next checks in"}
    # Neither here nor reachable: leave it with the CI host for the target to pick up.
    try:
        reply = call(ci_host, ['queue', host, action, json.dumps(args or {})], timeout=40, settings=settings)
        return dict(reply, via=f'outbox on {ci_host}')
    except (CommandError, ValueError) as e:
        return {'ok': False, 'via': ci_host, 'message': f'{ci_host}: {e}'}
