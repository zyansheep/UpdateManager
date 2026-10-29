"""Forks and vendored code against their upstreams.

check(fork) fetches the upstream, finds what to follow (the newest stable tag, or
a branch), and works out, without touching any worktree:

- behind / ahead: upstream commits the fork lacks, and the fork's own;
- whether it is a fast-forward;
- a merge preview (`git merge-tree --write-tree`): clean, or which files conflict;
- for a patch stack (strategy "rebase"), whether the fork's commits replay cleanly
  onto the new upstream (`git replay`).

apply(fork, how) does the clean ones: a fast-forward, a merge commit, or the
rebase, by moving the branch ref with compare-and-swap when the branch is not
checked out, or in its worktree when it is and that worktree is clean. It never
pushes; push() is a separate step. Conflicts go to an agent (agent.py).

Fetching only moves remote-tracking refs (or, for an upstream given as a URL,
refs/update-manager/upstream/* — a namespace `git branch -a` doesn't list).
"""
import fnmatch
import re
import time
from pathlib import Path

from . import config
from .profiles import version_key
from .util import CommandError, expand, run

PRIVATE = 'refs/update-manager/upstream'


def git(repo, *args, timeout=60, check=True, input=None):
    return run(['git', '-C', repo, *args], timeout=timeout, check=check, input=input)


def resolve_branch(repo, fork):
    """The fork's branch: `branch`, else the first fallback that exists. 'HEAD' means whatever is
    checked out (a submodule's detached HEAD)."""
    for name in [fork.get('branch', 'HEAD'), *fork.get('fallback_branches', [])]:
        if name == 'HEAD':
            return 'HEAD', git(repo, 'rev-parse', 'HEAD').strip()
        done = git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{name}^{{commit}}', check=False)
        if done.returncode == 0:
            return name, done.stdout.strip()
    raise CommandError(['git', 'rev-parse', fork.get('branch')], 1, f"no branch {fork.get('branch')}")


def fetch(repo, fork, timeout=180):
    upstream = fork['upstream']
    if '://' in upstream or upstream.startswith('git@'):
        branch = fork.get('upstream_branch', 'main')
        refspecs = [f'+refs/heads/{branch}:{PRIVATE}/{branch}']
        if fork.get('track') == 'tag':
            refspecs.append(f'+refs/tags/*:{PRIVATE}/tags/*')
        git(repo, 'fetch', '--quiet', '--no-tags', upstream, *refspecs, timeout=timeout)
    else:
        git(repo, 'fetch', '--quiet', '--tags', upstream, timeout=timeout)


def upstream_ref(repo, fork):
    """(label, ref) to follow: the newest tag matching `tags` (without `exclude`, e.g. '-' leaves out
    nightlies and prereleases), else upstream_branch on the upstream remote."""
    upstream = fork['upstream']
    url = '://' in upstream or upstream.startswith('git@')
    if fork.get('track') == 'tag':
        prefix = f'{PRIVATE}/tags/' if url else 'refs/tags/'
        names = git(repo, 'for-each-ref', '--format=%(refname)', prefix).split()
        pattern, exclude = fork.get('tags', '*'), fork.get('exclude')
        tags = [n[len(prefix):] for n in names]
        tags = [t for t in tags if fnmatch.fnmatch(t, pattern) and not (exclude and exclude in t)]
        if not tags:
            raise CommandError(['git', 'tag'], 1, f'no tag matches {pattern}')
        tag = max(tags, key=version_key)
        return tag, f'{prefix}{tag}^{{commit}}'
    branch = fork.get('upstream_branch', 'main')
    return (f'{upstream}/{branch}' if not url else branch,
            f'{PRIVATE}/{branch}' if url else f'refs/remotes/{upstream}/{branch}')


def count(repo, spec):
    return int(git(repo, 'rev-list', '--count', spec).strip() or 0)


def merge_preview(repo, ours, theirs):
    """(clean, tree, conflicted files) from git merge-tree --write-tree (git 2.38+)."""
    done = git(repo, 'merge-tree', '--write-tree', '--name-only', '--no-messages', ours, theirs,
               check=False, timeout=120)
    lines = done.stdout.splitlines()
    tree = lines[0].strip() if lines else None
    if done.returncode == 0:
        return True, tree, []
    if done.returncode == 1:
        return False, tree, [line for line in lines[1:] if line.strip()]
    raise CommandError(['git', 'merge-tree'], done.returncode, done.stderr)


def replay_preview(repo, base, branch_rev, onto):
    """Whether base..branch_rev replays onto `onto` without conflicts (git replay, 2.44+), writing
    only objects. Returns (clean, new head or None).

    git 2.52 made `git replay` move refs itself by default; --ref-action=print keeps it a preview
    (and older versions, which only print, reject the option: then run them without it)."""
    args = ['replay', '--onto', onto, f'{base}..{branch_rev}']
    done = git(repo, 'replay', '--ref-action=print', '--ref=refs/update-manager/replay', *args[1:],
               check=False, timeout=120)
    if done.returncode == 129:
        done = git(repo, *args, check=False, timeout=120)
    if done.returncode == 0:
        # "update <ref> <new> <old>"; the new commit is the replayed head.
        for line in reversed(done.stdout.splitlines()):
            parts = line.split()
            if len(parts) >= 3 and parts[0] == 'update':
                return True, parts[2]
        return True, None
    return False, None


def worktree_of(repo, branch):
    """The worktree that has branch checked out, if any: {'path', 'clean'}."""
    if branch == 'HEAD':
        path = git(repo, 'rev-parse', '--show-toplevel').strip()
    else:
        path, current = None, None
        for line in git(repo, 'worktree', 'list', '--porcelain').splitlines() + ['']:
            if line.startswith('worktree '):
                current = line[len('worktree '):]
            elif line == f'branch refs/heads/{branch}':
                path = current
        if not path:
            return None
    status = git(path, 'status', '--porcelain', '--untracked-files=no', check=False)
    return {'path': path, 'clean': status.returncode == 0 and not status.stdout.strip()}


def check(fork, do_fetch=True, now=None):
    """One fork's state. 'status' is one of: current, fast-forward, clean-merge, clean-rebase,
    conflicts, error, missing."""
    now = now or time.time()
    repo = expand(fork['repo'])
    out = {'name': fork['name'], 'repo': fork['repo'], 'strategy': fork.get('strategy', 'merge'),
           'checked': now, 'push': fork.get('push')}
    if not (repo / '.git').exists():
        return dict(out, status='missing', error=f"{fork['repo']} is not a git checkout")
    try:
        branch, head = resolve_branch(repo, fork)
        out['branch'], out['head'] = branch, head[:12]
        if do_fetch and fork.get('fetch', True):  # fetch = false: compare against what is fetched already
            try:
                fetch(repo, fork)
                out['fetched'] = now
            except CommandError as e:
                out['fetch_error'] = str(e)[:200]
        label, ref = upstream_ref(repo, fork)
        theirs = git(repo, 'rev-parse', ref).strip()
        out['upstream'], out['upstream_head'] = label, theirs[:12]
        out['behind'] = count(repo, f'{head}..{theirs}')
        out['ahead'] = count(repo, f'{theirs}..{head}')
        out['upstream_log'] = [line for line in git(repo, 'log', '--format=%h %s', '--max-count=8',
                                                    f'{head}..{theirs}').splitlines()]
        if fork.get('track') == 'tag':
            base = git(repo, 'describe', '--tags', '--abbrev=0', head, check=False).stdout.strip()
            out['based_on'] = base or None
        out['worktree'] = worktree_of(repo, branch)
        if fork.get('push') and branch != 'HEAD':
            pushed = git(repo, 'rev-parse', '--verify', '--quiet', f"refs/remotes/{fork['push']}/{branch}",
                         check=False)
            out['unpushed'] = count(repo, f'{pushed.stdout.strip()}..{head}') if pushed.returncode == 0 else None
        if out['behind'] == 0:
            out['status'] = 'current'
            return out
        if out['ahead'] == 0:
            out['status'] = 'fast-forward'
            return out
        if out['strategy'] == 'rebase':
            base = git(repo, 'merge-base', head, theirs).strip()
            clean, new_head = replay_preview(repo, base, head, theirs)
            if clean:
                out['status'], out['rebased'] = 'clean-rebase', new_head
                return out
        clean, tree, files = merge_preview(repo, head, theirs)
        if clean:
            out['status'], out['tree'] = 'clean-merge', tree
        else:
            out['status'], out['conflicts'] = 'conflicts', files[:200]
            out['conflict_count'] = len(files)
    except CommandError as e:
        out['status'], out['error'] = 'error', str(e)[:300]
    return out


def check_all(forks=None, do_fetch=True, settings=None):
    forks = forks if forks is not None else config.get('forks', settings=settings)
    return {'checked': time.time(), 'items': [check(f, do_fetch) for f in forks]}


def find(name, settings=None):
    for fork in config.get('forks', settings=settings) or []:
        if fork['name'] == name:
            return fork
    raise KeyError(name)


def apply(fork, how=None):
    """Bring the fork up to its upstream when that is clean: 'fast-forward', 'merge' or 'rebase'.
    Returns {'ok', 'message', 'old', 'new'}. Re-checks first, so the state it acts on is current."""
    state = check(fork, do_fetch=False)
    repo = expand(fork['repo'])
    status = state.get('status')
    how = how or {'fast-forward': 'fast-forward', 'clean-merge': 'merge', 'clean-rebase': 'rebase'}.get(status)
    if status == 'current':
        return {'ok': True, 'message': f"{fork['name']} is already up to date with {state['upstream']}"}
    if how is None or status in ('conflicts', 'error', 'missing'):
        return {'ok': False, 'message': f"{fork['name']}: {status}; not a clean update", 'state': state}
    branch, old = resolve_branch(repo, fork)
    _, ref = upstream_ref(repo, fork)
    theirs = git(repo, 'rev-parse', ref).strip()
    tree = state.get('worktree')
    if tree and not tree['clean']:
        return {'ok': False, 'message': f"{tree['path']} has uncommitted changes: commit or stash them first"}
    message = f"Merge {state['upstream']} into {branch}"
    if tree:  # Checked out: let git update the worktree too.
        wt = tree['path']
        if how == 'fast-forward':
            git(wt, 'merge', '--ff-only', theirs, timeout=300)
        elif how == 'merge':
            git(wt, 'merge', '--no-edit', '-m', message, theirs, timeout=300)
        elif how == 'rebase':
            done = git(wt, 'rebase', theirs, check=False, timeout=600)
            if done.returncode != 0:
                git(wt, 'rebase', '--abort', check=False)
                return {'ok': False, 'message': 'the rebase stopped on a conflict; aborted, nothing changed'}
        new = git(wt, 'rev-parse', 'HEAD').strip()
    else:
        if how == 'fast-forward':
            new = theirs
        elif how == 'merge':
            clean, merged_tree, _ = merge_preview(repo, old, theirs)
            if not clean:
                return {'ok': False, 'message': 'the merge is no longer clean'}
            new = git(repo, 'commit-tree', merged_tree, '-p', old, '-p', theirs, '-m', message).strip()
        else:
            base = git(repo, 'merge-base', old, theirs).strip()
            clean, new = replay_preview(repo, base, old, theirs)
            if not clean or not new:
                return {'ok': False, 'message': 'the rebase is no longer clean'}
        # Compare-and-swap: fails if the branch moved since we looked.
        git(repo, 'update-ref', '-m', f'update-manager: {how} {state["upstream"]}', f'refs/heads/{branch}', new, old)
    return {'ok': True, 'how': how, 'old': old[:12], 'new': new[:12],
            'message': f"{fork['name']}: {branch} {how} to {state['upstream']} ({old[:8]} → {new[:8]}); not pushed"}


def push(fork):
    """Push the fork's branch to its `push` remote (force-with-lease after a rebase)."""
    remote = fork.get('push')
    if not remote:
        return {'ok': False, 'message': f"{fork['name']}: pushing is off for this fork"}
    repo = expand(fork['repo'])
    branch, head = resolve_branch(repo, fork)
    if branch == 'HEAD':
        return {'ok': False, 'message': 'detached HEAD: push it by hand'}
    args = ['push', remote, f'{branch}:{branch}']
    if fork.get('strategy') == 'rebase':
        args.insert(1, '--force-with-lease')
    git(repo, *args, timeout=300)
    return {'ok': True, 'message': f"pushed {branch} ({head[:8]}) to {remote}"}


def brief(fork, state, host=None):
    """The task for an agent that ports a conflicting upstream update by hand."""
    host = host or config.hostname()
    repo = fork['repo']
    branch = state.get('branch') or fork.get('branch')
    lines = [
        f"# Bring {fork['name']} up to date with its upstream",
        '',
        f"Repository: `{repo}` on {host}. Fork branch: `{branch}` (at {state.get('head')}). "
        f"Upstream: `{fork['upstream']}`, following {state.get('upstream')} (at {state.get('upstream_head')}).",
        f"The fork is {state.get('ahead')} commits ahead and {state.get('behind')} behind."
        + (f" It is based on {state['based_on']}." if state.get('based_on') else ''),
        '',
        fork.get('notes') or '',
        '',
        '## What to do',
        '',
    ]
    if fork.get('strategy') == 'rebase':
        lines += [
            f"Rebase the fork's own commits onto {state.get('upstream')} in a new worktree on a new branch "
            f"(e.g. `{branch}-{state.get('upstream')}`), resolving each conflict so the patch keeps doing what it "
            "did. Keep the commits separate (one patch per commit, same messages).",
        ]
    else:
        lines += [
            f"Merge {state.get('upstream')} into `{branch}` in a new worktree on a new branch, resolving the "
            "conflicts. Where upstream changed code the fork rewrote, port the upstream change by hand into the "
            "fork's version instead of taking either side.",
        ]
    lines += [
        "Then run the project's tests and build, and fix what breaks. Do not push and do not touch the "
        f"`{branch}` branch itself: leave the result on the new branch and report what you changed, what you "
        "were unsure about, and what to check before merging it.",
        '',
        f"## Conflicting files ({state.get('conflict_count', len(state.get('conflicts') or []))})",
        '',
        *[f'- `{f}`' for f in (state.get('conflicts') or [])[:80]],
    ]
    if state.get('upstream_log'):
        lines += ['', '## Newest upstream commits', '', *[f'- {c}' for c in state['upstream_log']]]
    lines += ['', '_Written by update-manager._']
    return '\n'.join(line for line in lines if line is not None)


def patch_check(patch, settings=None, timeout=240):
    """A patched nixos-conf package against upstream nixpkgs: the version the lock builds and the
    version upstream has now. A different version means the patches may need rebasing."""
    flake = config.flake_dir(settings)
    host = config.get('patches_host', settings=settings)
    upstream = config.get('patches_upstream', settings=settings)
    attr = patch['attr']
    out = {'name': patch['name'], 'attr': attr, 'files': patch.get('files', []), 'where': patch.get('where'),
           'checked': time.time()}
    try:
        out['locked'] = run(['nix', 'eval', '--raw',
                             f'{flake}#nixosConfigurations.{host}.pkgs.{attr}.version'], timeout=timeout).strip()
        out['upstream'] = run(['nix', 'eval', '--raw', f'{upstream}#{attr}.version'], timeout=timeout).strip()
        out['moved'] = out['locked'] != out['upstream']
    except CommandError as e:
        out['error'] = str(e)[:300]
    missing = [f for f in out['files'] if not (Path(flake) / f).exists()]
    if missing:
        out['missing_files'] = missing
    return out


def patch_brief(patch_state, settings=None):
    flake = config.get('flake', settings=settings)
    return '\n'.join([
        f"# Check nixos-conf's {patch_state['name']} patches against {patch_state.get('upstream')}",
        '',
        f"`{flake}` patches {patch_state['attr']} (in `{patch_state.get('where')}`). The lock builds "
        f"{patch_state.get('locked')}; upstream nixpkgs now has {patch_state.get('upstream')}.",
        '',
        'Patches:', *[f'- `{f}`' for f in patch_state.get('files', [])],
        '',
        f"Update the flake input on a new branch, build `.#nixosConfigurations.isomorph.pkgs.{patch_state['attr']}` "
        "and rebase each patch that no longer applies onto the new source, keeping what it does. Run any test "
        "the override runs. Do not push; report what changed.",
    ])


def safe_name(text):
    return re.sub(r'[^A-Za-z0-9._-]+', '-', text).strip('-').lower()
