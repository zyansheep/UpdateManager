"""Hand a conflicting upstream merge to an agent: a T3 Code thread on this machine.

`t3 thread start <repo> - --worktree <branch> --branch <new> --json` (t3 0.1.4 and
later) signs in to the running T3 server as an MCP client and launches the thread
in a fresh worktree, so the agent never touches the fork's branch. Older t3 CLIs
(0.1.0 has no `thread` command, and its server only takes MCP client sessions,
not `t3 auth session issue` tokens) fall back to saving the brief, copying it to
the clipboard and saying so; paste it into a new thread in that project.
"""
import os
import shutil
import time

from . import config
from .util import CommandError, expand, run, write_json


def t3_binary(settings=None):
    for candidate in config.get('agent', 't3', settings=settings) or ['t3']:
        path = shutil.which(str(expand(candidate)))
        if path:
            return path
    return None


def supports_threads(t3):
    try:
        done = run([t3, 'thread', '--help'], timeout=20, check=False)
    except CommandError:
        return False
    text = (done.stdout or '') + (done.stderr or '')
    return done.returncode == 0 and 'start' in text and 'Run the T3 Code server' not in text


def save_brief(name, text):
    directory = config.STATE / 'briefs'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(text)
    return path


def copy_to_clipboard(text):
    if not os.environ.get('WAYLAND_DISPLAY') or not shutil.which('wl-copy'):
        return False
    try:
        run(['wl-copy'], input=text, timeout=5)
        return True
    except CommandError:
        return False


def launch(name, repo, base_ref, brief, title, settings=None):
    """Start the agent. Returns {'ok', 'method', 'thread'?, 'brief_path', 'message'}."""
    path = save_brief(name, brief)
    out = {'brief_path': str(path)}
    agent = config.get('agent', settings=settings) or {}
    t3 = t3_binary(settings) if agent.get('enabled', True) else None
    if t3 and supports_threads(t3):
        branch = f"update-manager/{name}-{time.strftime('%Y%m%d')}"
        args = [t3, 'thread', 'start', str(expand(repo)), '-', '--title', title, '--worktree', base_ref,
                '--branch', branch, '--json']
        if agent.get('provider') and agent.get('model'):
            args += ['--provider', agent['provider'], '--model', agent['model']]
        try:
            import json
            launched = json.loads(run(args, input=brief, timeout=120))
            out.update(ok=True, method='t3-thread', thread=launched.get('threadId'), branch=branch,
                       message=f"Started T3 thread {launched.get('threadId')} on {branch}")
            write_json(path.with_suffix('.json'), out)
            return out
        except (CommandError, ValueError) as e:
            out['t3_error'] = str(e)[:300]
    copied = copy_to_clipboard(brief)
    why = out.get('t3_error') or ("this t3 has no `thread` command (it needs t3 0.1.4 or later)" if t3
                                  else 'no t3 CLI found')
    out.update(ok=True, method='brief', copied=copied,
               message=(f"Brief saved to {path}" + (' and copied' if copied else '') +
                        f": paste it into a new T3 thread in {repo} ({why})"))
    write_json(path.with_suffix('.json'), out)
    return out
