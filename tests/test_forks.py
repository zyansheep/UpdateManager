import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from update_manager import forks

os.environ.update(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t',
                  GIT_CONFIG_NOSYSTEM='1')


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text, message=None):
    (Path(repo) / name).write_text(text)
    git(repo, 'add', name)
    git(repo, 'commit', '-qm', message or f'{name}: {text}')
    return git(repo, 'rev-parse', 'HEAD')


class ForkRepo:
    """An upstream repository and a fork of it (with `upstream` as a remote)."""

    def __init__(self):
        root = Path(tempfile.mkdtemp())
        self.up, self.fork = root / 'up', root / 'fork'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.up)], check=True)
        commit(self.up, 'a', '1\n2\n3\n')
        git(self.up, 'tag', 'v1.0.0')
        subprocess.run(['git', 'clone', '-q', str(self.up), str(self.fork)], check=True)
        git(self.fork, 'remote', 'rename', 'origin', 'upstream')
        git(self.fork, 'checkout', '-qb', 'patched')

    def config(self, **kw):
        return dict({'name': 'f', 'repo': str(self.fork), 'branch': 'patched', 'upstream': 'upstream',
                     'track': 'branch', 'upstream_branch': 'main', 'strategy': 'merge'}, **kw)


class ForksTest(unittest.TestCase):
    def test_current_and_fast_forward(self):
        r = ForkRepo()
        self.assertEqual(forks.check(r.config())['status'], 'current')
        commit(r.up, 'b', 'new\n')
        state = forks.check(r.config())
        self.assertEqual((state['status'], state['behind'], state['ahead']), ('fast-forward', 1, 0))
        result = forks.apply(r.config())
        self.assertTrue(result['ok'], result)
        self.assertEqual(git(r.fork, 'rev-parse', 'patched'), git(r.up, 'rev-parse', 'main'))
        self.assertTrue((r.fork / 'b').exists())  # checked out: the worktree moved too

    def test_clean_merge_not_checked_out(self):
        r = ForkRepo()
        commit(r.fork, 'mine', 'x\n')
        git(r.fork, 'checkout', '-q', '--detach')  # the branch is no longer checked out
        commit(r.up, 'theirs', 'y\n')
        state = forks.check(r.config())
        self.assertEqual(state['status'], 'clean-merge')
        self.assertIsNone(state['worktree'])
        old = git(r.fork, 'rev-parse', 'patched')
        result = forks.apply(r.config())
        self.assertTrue(result['ok'], result)
        merged = git(r.fork, 'rev-parse', 'patched')
        self.assertNotEqual(merged, old)
        self.assertEqual(len(git(r.fork, 'rev-list', '--parents', '-n1', merged).split()), 3)  # a merge commit
        self.assertEqual(forks.check(r.config(), do_fetch=False)['status'], 'current')

    def test_conflicts_and_brief(self):
        r = ForkRepo()
        commit(r.fork, 'a', '1\nmine\n3\n')
        commit(r.up, 'a', '1\ntheirs\n3\n')
        state = forks.check(r.config())
        self.assertEqual(state['status'], 'conflicts')
        self.assertEqual(state['conflicts'], ['a'])
        self.assertFalse(forks.apply(r.config())['ok'])
        brief = forks.brief(r.config(notes='Keep the fork.'), state, host='desktop')
        self.assertIn('`a`', brief)
        self.assertIn('Keep the fork.', brief)
        self.assertIn('Do not push', brief)

    def test_tag_tracking_and_clean_rebase(self):
        r = ForkRepo()
        commit(r.fork, 'patch1', 'p\n')
        commit(r.fork, 'patch2', 'q\n')
        commit(r.up, 'b', 'b\n')
        git(r.up, 'tag', 'v1.1.0')
        commit(r.up, 'c', 'c\n')
        git(r.up, 'tag', 'v1.2.0-nightly.1')
        cfg = r.config(track='tag', tags='v[0-9]*', exclude='-', strategy='rebase')
        before = git(r.fork, 'rev-parse', 'patched')
        state = forks.check(cfg)
        self.assertEqual((state['upstream'], state['based_on']), ('v1.1.0', 'v1.0.0'))
        self.assertEqual(state['status'], 'clean-rebase')
        self.assertEqual(git(r.fork, 'rev-parse', 'patched'), before)  # the preview moved nothing
        result = forks.apply(cfg)
        self.assertTrue(result['ok'], result)
        self.assertEqual(git(r.fork, 'describe', '--tags', '--abbrev=0', 'patched'), 'v1.1.0')
        self.assertEqual(git(r.fork, 'rev-list', '--count', 'v1.1.0..patched'), '2')

    def test_dirty_worktree_refuses(self):
        r = ForkRepo()
        commit(r.fork, 'mine', 'x\n')
        commit(r.up, 'theirs', 'y\n')
        (r.fork / 'a').write_text('edited\n')
        self.assertEqual(forks.check(r.config())['status'], 'clean-merge')
        result = forks.apply(r.config())
        self.assertFalse(result['ok'])
        self.assertIn('uncommitted', result['message'])

    def test_upstream_as_url_uses_private_refs(self):
        r = ForkRepo()
        commit(r.up, 'b', 'b\n')
        cfg = r.config(upstream=str(r.up.as_uri()))
        state = forks.check(cfg)
        self.assertEqual(state['status'], 'fast-forward')
        self.assertIn(f'{forks.PRIVATE}/main', git(r.fork, 'for-each-ref', '--format=%(refname)'))
        self.assertNotIn('update-manager', git(r.fork, 'branch', '-a'))

    def test_missing(self):
        self.assertEqual(forks.check({'name': 'x', 'repo': '/nonexistent', 'upstream': 'u'})['status'], 'missing')
