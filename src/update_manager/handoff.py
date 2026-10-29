"""Hand a conflicting upstream merge to an agent: a T3 Code thread on this machine.

In order:
1. `t3 thread start <repo> - --json` when the t3 CLI has it (0.1.4 and later);
2. T3's MCP endpoint, signed in as an outside agent (t3mcp.py): find the
   repository's project by its workspaceRoot (or register it), then
   t3_thread_launch with the brief. This works with t3 0.1.0;
3. otherwise save the brief, copy it to this machine's clipboard and say so.

The thread runs in the repository's own checkout (workspaceStrategy root);
the brief has the agent do its work in a `git worktree` beside it.
"""
import json
import os
import shutil
import subprocess
import time

from . import config, t3mcp
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


def notify(summary, body):
    if os.environ.get('WAYLAND_DISPLAY') and shutil.which('notify-send'):
        subprocess.run(['notify-send', '--app-name=Updates', summary, body], capture_output=True)


def via_cli(t3, repo, brief, title, settings=None):
    args = [t3, 'thread', 'start', str(expand(repo)), '-', '--title', title, '--json']
    selection = t3mcp.model_selection(settings)
    if selection.get('instanceId') and selection.get('model'):
        args += ['--provider', selection['instanceId'], '--model', selection['model']]
    launched = json.loads(run(args, input=brief, timeout=120))
    return {'thread': launched.get('threadId'), 'title': title, 'projectId': launched.get('projectId')}


def launch(name, repo, brief, title, project_title=None, settings=None):
    """Start the agent. Returns {'ok', 'method', 'thread'?, 'title', 'project'?, 'brief_path', 'message', 'tried'}."""
    path = save_brief(name, brief)
    out = {'brief_path': str(path), 'title': title, 'tried': []}
    agent = config.get('agent', settings=settings) or {}
    if agent.get('enabled', True):
        t3 = t3_binary(settings)
        if t3 and supports_threads(t3):
            try:
                out.update(via_cli(t3, repo, brief, title, settings), ok=True, method='t3-thread')
                out['message'] = f"Started the T3 thread “{title}” in {repo}"
                write_json(path.with_suffix('.json'), out)
                return out
            except (CommandError, ValueError) as e:
                out['tried'].append(f't3 thread start: {str(e)[:200]}')
        try:
            started = t3mcp.launch(repo, project_title or name, title, brief, settings)
            out.update(started, ok=True, method='mcp')
            out['message'] = (f"Started the T3 thread “{title}” in the project “{started['project']}”"
                              + (' (new project)' if started.get('created_project') else ''))
            write_json(path.with_suffix('.json'), out)
            return out
        except (t3mcp.T3Error, CommandError, ValueError, KeyError) as e:
            out['tried'].append(f'T3 MCP: {str(e)[:200]}')
    copied = copy_to_clipboard(brief)
    why = '; '.join(out['tried']) or 'agent handoff is off (agent.enabled)'
    out.update(ok=True, method='brief', copied=copied,
               message=(f"No T3 thread ({why}). The brief is in {path}" + (', copied to the clipboard' if copied
                                                                           else '')
                        + f": paste it into a new thread in {repo}"))
    notify(f'Brief for {title}', out['message'])
    write_json(path.with_suffix('.json'), out)
    return out
