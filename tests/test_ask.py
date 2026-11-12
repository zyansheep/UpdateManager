import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from update_manager import ask, config, jobs, system


def script(path, body):
    path.write_text(f'#!{sys.executable}\nimport os, sys, json, pathlib\n{body}\n')
    path.chmod(0o755)
    return str(path)


real_start = jobs.start


class AskRebuildTest(unittest.TestCase):
    """The ask-rebuild job with the build, the window and the switch stubbed, in a throwaway checkout."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='ask-'))
        self.repo = self.root / 'conf'
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                   GIT_COMMITTER_EMAIL='t@t')
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'flake.nix').write_text('a\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-qm', 'one'], check=True, env=env)
        (self.repo / 'flake.nix').write_text('b\n')  # uncommitted
        (self.root / 'old').mkdir()
        (self.root / 'new').mkdir()
        self.calls = self.root / 'calls'
        self.answers = self.root / 'answers'  # one JSON answer per line, for each window
        self.next = self.root / 'next'  # what the build links ./result to
        self.next.write_text(str(self.root / 'new'))
        preview = script(self.root / 'preview', f"""
open({str(self.calls)!r}, 'a').write(json.dumps(['preview', *sys.argv[1:]]) + '\\n')
if os.path.exists({str(self.root / 'preview-fail')!r}): sys.exit(3)
os.symlink(open({str(self.next)!r}).read(), 'result')""")
        switch = script(self.root / 'switch', f"""
open({str(self.calls)!r}, 'a').write(json.dumps(['switch', *sys.argv[1:]]) + '\\n')
print('switching…')
sys.exit(4 if os.path.exists({str(self.root / 'switch-fail')!r}) else 0)""")
        window = script(self.root / 'window', f"""
request = json.load(open(sys.argv[1]))
open({str(self.calls)!r}, 'a').write(json.dumps(['window', request]) + '\\n')
lines = open({str(self.answers)!r}).read().splitlines()
open({str(self.answers)!r}, 'w').write(''.join(l + '\\n' for l in lines[1:]))
answer = json.loads(lines[0])
if answer.pop('touch', None): pathlib.Path({str(self.repo / 'new-file')!r}).write_text('c')
if answer.pop('fail', None): sys.exit(1)
print(json.dumps(answer))""")
        self.saved = config._loaded
        config._loaded = config.merge(config.DEFAULTS, {
            'flake': str(self.repo), 'ask': {'preview': [preview], 'switch': [switch],
                                             'current': str(self.root / 'old'), 'timeout': 30}})
        os.environ['UPDATE_MANAGER_ASK_WINDOW'] = window
        patches = [mock.patch.object(system, 'path_infos', side_effect=lambda p: {
                       f'/nix/store/{"a" * 32}-hello-{"1" if p.endswith("old") else "2"}': {'narSize': 10}}),
                   mock.patch.object(system, 'configuration_revision', return_value=None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        config._loaded = self.saved
        os.environ.pop('UPDATE_MANAGER_ASK_WINDOW', None)

    def run_job(self, *answers, args=()):
        self.answers.write_text(''.join(json.dumps(a) + '\n' for a in answers))
        job = jobs.start('ask-rebuild', {'reason': 'fix <the> clock', 'args': list(args), 'from': '/x'}, wait=True)
        self.log = jobs.job_path(job['id']).with_suffix('.log').read_text()
        return job

    def calls_of(self, kind):
        lines = self.calls.read_text().splitlines() if self.calls.exists() else []
        return [json.loads(line)[1:] for line in lines if json.loads(line)[0] == kind]

    def test_validates(self):
        for bad in ({}, {'reason': ' '}, {'reason': 'x' * 2001}, {'reason': 'r', 'args': 'not a list'},
                    {'reason': 'r', 'args': ['a\0b']}):
            with self.assertRaises(jobs.ActionError):
                jobs.validate('ask-rebuild', bad)
        kind, title, args = jobs.validate('ask-rebuild', {'reason': 'two\nlines', 'args': ['-L'], 'x': 1})
        self.assertEqual((kind, title, args), ('ask-rebuild', 'Ask to rebuild: two',
                                                {'reason': 'two\nlines', 'args': ['-L'], 'from': None}))

    def test_approve_switches_with_the_args(self):
        job = self.run_job({'decision': 'approve', 'message': 'go ahead'}, args=['--override-input', 'a', 'path:/a b'])
        self.assertEqual(job['state'], 'done', job.get('message'))
        self.assertEqual(job['outcome'], 'switched')
        self.assertEqual(job['answer'], {'decision': 'approve', 'message': 'go ahead'})
        self.assertIn('go ahead', job['message'])
        self.assertEqual(self.calls_of('switch'), [['--override-input', 'a', 'path:/a b']])
        self.assertEqual(self.calls_of('preview'), [['--override-input', 'a', 'path:/a b']])
        request = self.calls_of('window')[0][0]
        self.assertEqual(request['reason'], 'fix <the> clock')
        self.assertEqual(request['from'], '/x')
        self.assertEqual(request['diff']['upgraded'], 1)
        self.assertEqual(request['uncommitted'], ['M\tflake.nix'])
        self.assertIsNone(request['commits'])
        self.assertIn('switching…', self.log)
        self.assertEqual(subprocess.run(['git', '-C', str(self.repo), 'diff', '--name-only'],
                                        capture_output=True, text=True).stdout, '')  # staged, as nrb does
        self.assertFalse((config.RUNTIME / 'ask' / job['id']).exists())

    def test_deny_and_timeout_and_no_window(self):
        job = self.run_job({'decision': 'deny', 'message': 'not now'})
        self.assertEqual((job['state'], job['outcome'], job['message']), ('failed', 'denied', 'denied: not now'))
        job = self.run_job({'decision': 'timeout', 'message': ''})
        self.assertEqual((job['state'], job['outcome']), ('failed', 'timeout'))
        job = self.run_job({'fail': True})
        self.assertEqual((job['state'], job['outcome']), ('failed', 'no-window'))
        self.assertEqual(self.calls_of('switch'), [])

    def test_build_failure_and_nothing_to_switch(self):
        (self.root / 'preview-fail').touch()
        job = self.run_job({'decision': 'approve'})
        self.assertEqual((job['state'], job['outcome']), ('failed', 'build-failed'))
        (self.root / 'preview-fail').unlink()
        self.next.write_text(str(self.root / 'old'))
        job = self.run_job({'decision': 'approve'})
        self.assertEqual((job['state'], job['outcome']), ('done', 'current'))
        self.assertEqual(self.calls_of('window') + self.calls_of('switch'), [])

    def test_a_change_while_asking_asks_again(self):
        job = self.run_job({'decision': 'approve', 'touch': True}, {'decision': 'approve'})
        self.assertEqual(job['outcome'], 'switched')
        windows = self.calls_of('window')
        self.assertEqual(len(windows), 2)
        self.assertIsNone(windows[0][0]['note'])
        self.assertIn('changed', windows[1][0]['note'])
        self.assertEqual(len(self.calls_of('switch')), 1)

    def test_switch_failure(self):
        (self.root / 'switch-fail').touch()
        job = self.run_job({'decision': 'approve'})
        self.assertEqual((job['state'], job['outcome']), ('failed', 'switch-failed'))

    def test_the_command_waits_and_exits_by_outcome(self):
        for answer, code in (({'decision': 'approve', 'message': 'ok'}, 0), ({'decision': 'deny', 'message': 'no'}, 77),
                             ({'decision': 'timeout'}, 75)):
            self.answers.write_text(json.dumps(answer) + '\n')
            out, err = io.StringIO(), io.StringIO()
            out.buffer = io.BytesIO()  # follow() copies the log as bytes
            # The job runs in this process, so follow() finds it ended.
            with mock.patch.object(jobs, 'start', side_effect=lambda a, args, by=None: real_start(a, args, by=by, wait=True)), \
                    redirect_stdout(out), redirect_stderr(err):
                result = ask.main(['why', '-L'])
            self.assertEqual(result, code, err.getvalue())
            self.assertIn(b'ask-rebuild: request: why', out.buffer.getvalue())
            if answer.get('message'):
                self.assertIn(f"The user's message: {answer['message']}", out.getvalue())
        self.assertEqual([c[0] for c in self.calls_of('switch')], ['-L'])

    def test_usage(self):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(ask.main([]), 64)
            self.assertEqual(ask.main(['--help']), 0)
            self.assertEqual(ask.main(['-x']), 64)


if __name__ == '__main__':
    unittest.main()
