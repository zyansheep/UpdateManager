"""The resident agent (update-manager-agent.service, one per machine).

It is what makes the widget instant, the way drainhound's estimator is: it does
every slow thing in the background and publishes the result as one small JSON
file, $XDG_RUNTIME_DIR/update-manager/state.json (and a copy in
~/.cache/update-manager that outlives a reboot). The Waybar module and the
resident panel only read that file.

Three loops:
- fast (every 15 s, 3 s while something runs): system, nixos-ci, jobs; publish.
- peers (every 30 s, 8 s while a peer is busy): one ssh exchange per machine
  that takes connections; run what was queued for us.
- slow (every 15 min, or at once on `update-manager refresh`): closure diff and
  download estimate, git fetch of the checkout, then less often flake input
  freshness, profile updates, upstream checks and patched packages.
"""
import threading
import time
import traceback

from . import collect, config, jobs, mock, remote, system
from .util import CommandError, read_json, write_json

LINKS = config.RUNTIME / 'links.json'
DUE = config.CACHE / 'due.json'


class Agent:
    def __init__(self, settings=None, mock_peers=(), once=False):
        self.settings = settings or config.load()
        self.me = config.hostname()
        self.mock_peers = [p for p in mock_peers if p != self.me]
        self.once = once
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.links = read_json(LINKS, {}) or {}
        self.busy_peers = set()
        self.own = None

    # --- helpers -------------------------------------------------------------------
    def interval(self, name):
        return config.get('intervals', name, settings=self.settings)

    def peers(self):
        return [h for h, entry in (config.get('hosts', settings=self.settings) or {}).items()
                if h != self.me and entry.get('pull', True) and h not in self.mock_peers]

    def peer_ci(self):
        ci_host = config.get('ci_host', settings=self.settings)
        if ci_host == self.me:
            return None
        return ((remote.peer_state(ci_host) or {}).get('state') or {}).get('ci')

    def peer_entries(self):
        entries = {}
        for name in config.get('hosts', settings=self.settings) or {}:
            if name == self.me:
                continue
            entry = remote.peer_state(name)
            if entry:
                pulled = name in self.peers()
                entries[name] = dict(entry, source='mock' if name in self.mock_peers else
                                     ('pulled' if pulled else 'pushed'), link=self.links.get(name))
        return entries

    def busy(self, view):
        s = view.get('summary', {})
        return bool(s.get('running') or s.get('ci_running'))

    # --- loops ---------------------------------------------------------------------
    def fast(self, compute=False):
        own = collect.own_state(compute=compute, settings=self.settings, peer_ci=self.peer_ci())
        with self.lock:
            self.own = own
        write_json(collect.OWN, own)
        view = collect.view(own, self.peer_entries(), settings=self.settings)
        collect.publish(view)
        return view

    def due(self, task, every):
        stamps = read_json(DUE, {}) or {}
        return time.time() - stamps.get(task, 0) >= every

    def mark(self, task):
        stamps = read_json(DUE, {}) or {}
        stamps[task] = time.time()
        write_json(DUE, stamps)

    def slow(self, forced=False):
        """The slow work, in order of what the panel needs most."""
        s = self.settings
        tasks = []
        if (config.flake_dir(s) / '.git').exists():
            tasks.append(('fetch', 900, lambda: system.fetch_checkout(s)))
        tasks.append(('update', 0, lambda: self.fast(compute=True)))
        for name in self.mock_peers:
            tasks.append((f'mock-{name}', 0, lambda n=name: self.make_mock(n)))
        if self.me == config.get('ci_host', settings=s):
            tasks.append(('inputs', self.interval('inputs'), self.check_inputs))
        tasks.append(('profiles', self.interval('profiles'), self.check_profiles))
        if self.me == config.get('forks_host', settings=s):
            tasks.append(('forks', self.interval('forks'), self.check_forks))
            tasks.append(('patches', self.interval('forks'), self.check_patches))
        for name, every, fn in tasks:
            if not forced and every and not self.due(name, every):
                continue
            try:
                fn()
                self.mark(name)
            except (CommandError, OSError, ValueError, KeyError) as e:
                print(f'update-manager agent: {name}: {e}', flush=True)
            except Exception:  # noqa: BLE001 - one broken check must not stop the others
                traceback.print_exc()
        self.fast()

    def check_forks(self):
        from . import forks
        write_json(config.CACHE / 'forks.json', forks.check_all(settings=self.settings))

    def check_patches(self):
        from . import forks
        write_json(config.CACHE / 'patches.json', {
            'checked': time.time(),
            'items': [forks.patch_check(p, self.settings) for p in config.get('patches', settings=self.settings) or []]})

    def check_inputs(self):
        from pathlib import Path
        lock = Path(config.get('nixos_ci', 'state', settings=self.settings)) / 'checkout-main/flake.lock'
        if not lock.exists():
            lock = system.lock_path_for(self.settings)
        items = system.input_freshness(system.flake_inputs(lock))
        write_json(config.CACHE / 'inputs.json', {'checked': time.time(), 'items': items})

    def check_profiles(self):
        from . import profiles
        previous = read_json(config.CACHE / 'profiles.json')
        write_json(config.CACHE / 'profiles.json', profiles.check(previous))

    def make_mock(self, name):
        ci_state = (self.own or {}).get('ci') or self.peer_ci()
        remote.save_peer(name, mock.own_state(name, ci_state, settings=self.settings))
        self.links[name] = {'ok': True, 'mock': True, 'last_ok': time.time()}

    def exchange_all(self):
        """One exchange with each machine that takes connections; run what it queued for us."""
        own = self.own or read_json(collect.OWN) or collect.own_state(compute=False, settings=self.settings)
        for name in self.peers():
            link = self.links.setdefault(name, {})
            link['last_try'] = time.time()
            started = time.time()
            try:
                reply = remote.exchange(name, own, settings=self.settings)
                state = reply.get('state') or {}
                remote.save_peer(name, state)
                link.update(ok=True, last_ok=time.time(), latency=round(time.time() - started, 2), error=None)
                for request in reply.get('requests') or []:
                    self.run_request(name, request)
                peer_busy = any(j.get('state') in ('running', 'queued') for j in
                                (state.get('self') or {}).get('jobs') or []) or (state.get('ci') or {}).get('running')
                (self.busy_peers.add if peer_busy else self.busy_peers.discard)(name)
            except (CommandError, ValueError) as e:
                link.update(ok=False, error=str(e)[:200])
                self.busy_peers.discard(name)
        write_json(LINKS, self.links)

    def run_request(self, sender, request):
        try:
            job = jobs.start(request['action'], request.get('args'), by=f"{sender} (queued {request.get('id')})")
            print(f"update-manager agent: {sender} asked for {request['action']}: job {job['id']}", flush=True)
        except (jobs.ActionError, CommandError) as e:
            print(f"update-manager agent: {sender}'s {request.get('action')}: {e}", flush=True)

    # --- main ------------------------------------------------------------------------
    def loop(self, name, fn, every):
        while True:
            try:
                fn()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            if self.once:
                return
            time.sleep(every())

    def run(self):
        config.RUNTIME.mkdir(parents=True, exist_ok=True)
        view = self.fast()
        if self.once:
            self.exchange_all()
            self.slow(forced=True)
            self.fast()
            return 0
        threading.Thread(target=self.loop, daemon=True, args=(
            'peers', self.exchange_all,
            lambda: self.interval('peer_busy') if self.busy_peers else self.interval('peer'))).start()
        threading.Thread(target=self.slow_loop, daemon=True).start()
        refresh = config.RUNTIME / 'refresh'
        while True:
            busy = self.busy(view)
            deadline = time.time() + (self.interval('busy') if busy else self.interval('fast'))
            while time.time() < deadline:
                if refresh.exists():
                    break
                time.sleep(0.5)
            try:
                view = self.fast()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def slow_loop(self):
        refresh = config.RUNTIME / 'refresh'
        self.slow()
        last = time.time()
        while True:
            time.sleep(1)
            forced = refresh.exists()
            if forced:
                try:
                    refresh.unlink()
                except OSError:
                    pass
            if forced or time.time() - last >= self.interval('slow'):
                try:
                    self.exchange_all()
                    self.slow(forced=forced)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
                last = time.time()


def main(argv):
    mock_peers, once = [], False
    it = iter(argv)
    for arg in it:
        if arg == '--mock-peer':
            mock_peers.append(next(it))
        elif arg == '--once':
            once = True
        else:
            print(f'usage: update-manager agent [--mock-peer HOST]... [--once] ({arg}?)')
            return 2
    import os
    mock_peers += [p for p in os.environ.get('UPDATE_MANAGER_MOCK_PEERS', '').split(',') if p]
    return Agent(mock_peers=mock_peers, once=once).run() or 0
