import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from update_manager import ask, config, jobs, system


def script(path, body):
    path.write_text(f'#!{sys.executable}\nimport os, sys, json, pathlib, time\n{body}\n')
    path.chmod(0o755)
    return str(path)


real_start = jobs.start


class Checkout(unittest.TestCase):
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
if answer.pop('block', None):  # until the job closes it
    import signal, time
    signal.signal(signal.SIGTERM, lambda *_: (open({str(self.calls)!r}, 'a').write('["window-closed"]\\n'), sys.exit(0)))
    time.sleep(60)
    sys.exit(1)
time.sleep(answer.pop('after', 0)) if 'after' in answer else None
print(json.dumps(answer))""")
        self.saved = config._loaded
        config._loaded = config.merge(config.DEFAULTS, {
            'flake': str(self.repo), 'ask': {'preview': [preview], 'switch': [switch],
                                             'current': str(self.root / 'old'), 'timeout': 30,
                                             'dither': 'off'}})
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



class AskRebuildTest(Checkout):
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
        self.assertEqual(job['answer'], {'decision': 'approve', 'message': 'go ahead', 'via': 'window'})
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



class DitherTest(Checkout):
    """Asking through dither-chat's approvals, with a fake dither-ask (it never reaches a hub)."""

    def setUp(self):
        super().setUp()
        os.environ.pop('UPDATE_MANAGER_ASK_WINDOW')  # no graphical session unless a test says so
        self.window = os.environ.get('UPDATE_MANAGER_ASK_WINDOW') or str(self.root / 'window')
        self.requests = self.root / 'dither-requests.json'
        self.dither_answers = self.root / 'dither-answers'  # one JSON line for each `status`
        self.dither_answers.write_text('')
        self.unreachable = self.root / 'unreachable'
        (self.root / 'token').write_text('tok\n')
        (self.root / 'keys').write_text('dev_x phone MFkw 3f:a1 Pixel 8\n')
        fake = script(self.root / 'dither-ask', f"""
import hashlib
args = sys.argv[1:]
open({str(self.calls)!r}, 'a').write(json.dumps(['dither', *args]) + '\\n')
def opt(name):
    return args[args.index(name) + 1] if name in args else None
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()
state = pathlib.Path({str(self.requests)!r})
cmd = args[0]
if cmd == 'diff-hash':
    print(sha('diff ' + opt('--base')))
    sys.exit(0)
assert opt('--url') == 'http://hub.invalid' and opt('--token-file') == {str(self.root / 'token')!r}, args
if os.path.exists({str(self.unreachable)!r}):
    print('the hub at http://hub.invalid cannot be reached', file=sys.stderr)
    sys.exit(69)
if cmd == 'request':
    reqs = json.loads(state.read_text()) if state.exists() else []
    rid = 'apv_' + str(len(reqs)).rjust(22, 'A')
    reqs.append({{'id': rid, 'title': opt('--title'), 'body': open(opt('--body-file')).read(),
                 'payload': open(opt('--payload-file')).read(), 'expires': opt('--expires')}})
    state.write_text(json.dumps(reqs))
    print(json.dumps({{'id': rid, 'kind': 'rebuild', 'state': 'waiting', 'action': None}}, indent=2))
    sys.exit(75)
if cmd == 'cancel':
    print(json.dumps({{'id': args[1], 'state': 'cancelled'}}))
    sys.exit(0)
assert cmd == 'status' and opt('--keys') == {str(self.root / 'keys')!r} and opt('--wait'), args
rid = args[1]
req = next(r for r in json.loads(state.read_text()) if r['id'] == rid)
lines = open({str(self.dither_answers)!r}).read().splitlines()
if not lines:
    time.sleep(min(float(opt('--wait')), 60))
    print(json.dumps({{'id': rid, 'state': 'waiting'}}))
    sys.exit(75)
open({str(self.dither_answers)!r}, 'w').write(''.join(l + '\\n' for l in lines[1:]))
a = json.loads(lines[0])
time.sleep(a.get('after', 0))
if a.get('block'):
    time.sleep(60)
    sys.exit(75)
if a.get('touch'):
    pathlib.Path({str(self.repo / 'new-file')!r}).write_text('c')
if a.get('code') == 69:
    print('the hub at http://hub.invalid cannot be reached', file=sys.stderr)
    sys.exit(69)
if 'state' in a:
    print(json.dumps({{'id': rid, 'state': a['state']}}))
    sys.exit(75)
action, message = a.get('action', 'approve'), a.get('message')
out = {{'id': rid, 'kind': 'rebuild', 'state': 'answered', 'action': action, 'message': message,
       'signed': action != 'deny', 'device': {{'id': 'dev_x', 'name': 'Pixel 8', 'kind': 'phone'}}}}
if action != 'deny':
    payload = 'something else' if a.get('forge') == 'payload' else req['payload']
    fields = [('id', rid), ('kind', 'rebuild'), ('asker', 'functor'), ('created', '1'), ('expires', '2'),
              ('payload-sha256', sha(payload)), ('display-sha256', sha(req['title'] + '\\n\\n' + req['body'])),
              ('action', action), ('message-sha256', sha(message or '')), ('device', 'dev_x'), ('signed', '3')]
    out.update(statement='dither-approval-v1\\n' + ''.join(f'{{k}} {{v}}\\n' for k, v in fields),
               signature='sig', verified=a.get('code') != 76)
print(json.dumps(out, indent=2))
sys.exit(a.get('code', 77 if action == 'deny' else 0))""")
        config._loaded['ask'].update(dither='on', dither_command=[fake], dither_url='http://hub.invalid',
                                     dither_token_file=str(self.root / 'token'), dither_keys=str(self.root / 'keys'))

    def with_window(self):
        os.environ['UPDATE_MANAGER_ASK_WINDOW'] = str(self.root / 'window')

    def run_job(self, *answers, window=(), args=()):
        self.dither_answers.write_text(''.join(json.dumps(a) + '\n' for a in answers))
        started = time.monotonic()
        job = super().run_job(*window, args=args)
        self.took = time.monotonic() - started
        return job

    def asked(self):
        return json.loads(self.requests.read_text()) if self.requests.exists() else []

    def head(self):
        return subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], capture_output=True,
                              text=True).stdout.strip()

    def test_the_phone_alone_approves(self):
        job = self.run_job({'action': 'approve', 'message': 'from the phone'})
        self.assertEqual((job['state'], job['outcome']), ('done', 'switched'), job.get('message'))
        self.assertEqual(job['answer'], {'decision': 'approve', 'message': 'from the phone', 'via': 'dither',
                                         'device': 'Pixel 8'})
        self.assertEqual(len(self.calls_of('switch')), 1)
        [req] = self.asked()
        head = self.head()
        self.assertEqual(req['payload'], f"dither-rebuild-v1\nhost laptop\nrunning {head}\n"
                         f"diff-sha256 {hashlib.sha256(f'diff {head}'.encode()).hexdigest()}\n"
                         f"system {self.root / 'new'}\n")
        self.assertEqual(req['title'], 'Switch laptop to a new build? fix <the> clock')
        self.assertEqual(req['expires'], '30')
        self.assertIn('fix <the> clock', req['body'])
        self.assertIn('(from /x)', req['body'])
        self.assertIn("The running system's commit is unknown", req['body'])  # configuration_revision: None
        self.assertIn('M flake.nix', req['body'])
        self.assertIn('1 upgraded', req['body'])
        self.assertEqual(self.calls_of('window'), [])
        self.assertNotIn('cancel', [c[0] for c in self.calls_of('dither')])
        self.assertIn('approved on Pixel 8: from the phone', self.log)

    def test_the_phone_denies_and_the_command_says_so(self):
        self.dither_answers.write_text(json.dumps({'action': 'deny', 'message': 'not today'}) + '\n')
        out, err = io.StringIO(), io.StringIO()
        out.buffer = io.BytesIO()
        with mock.patch.object(jobs, 'start', side_effect=lambda a, args, by=None: real_start(a, args, by=by, wait=True)), \
                redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(ask.main(['why']), 77)
        self.assertIn("The user's message: not today", out.getvalue())
        self.assertEqual(self.calls_of('switch'), [])

    def test_answers_that_do_not_verify_count_as_none(self):
        job = self.run_job({'action': 'approve', 'code': 76})
        self.assertEqual((job['state'], job['outcome']), ('failed', 'timeout'))
        self.assertIn('did not verify', job['message'])
        self.assertIn('!!!', self.log)
        job = self.run_job({'action': 'approve', 'forge': 'payload'})  # dither-ask said verified, but not for this
        self.assertEqual((job['state'], job['outcome']), ('failed', 'timeout'))
        self.assertIn('payload-sha256', job['message'])
        job = self.run_job({'state': 'expired'})
        self.assertEqual((job['state'], job['outcome'], job['message']), ('failed', 'timeout',
                                                                         'no answer in time, not switching'))
        self.assertEqual(self.calls_of('switch'), [])

    def test_the_hub_going_away_while_waiting(self):
        with mock.patch.object(ask, 'RETRY_S', 0.3):
            job = self.run_job({'code': 69}, {'state': 'waiting'}, {'action': 'approve'})
        self.assertEqual(job['outcome'], 'switched')
        self.assertIn("dither-chat can't be reached while waiting", self.log)
        self.assertEqual(len([c for c in self.calls_of('dither') if c[0] == 'status']), 3)

    def test_an_unreachable_hub_leaves_the_window(self):
        self.unreachable.touch()
        job = self.run_job()
        self.assertEqual((job['state'], job['outcome']), ('failed', 'no-window'))
        self.assertIn('nowhere to ask', job['message'])
        self.with_window()
        job = self.run_job(window=[{'decision': 'approve', 'message': 'ok'}])
        self.assertEqual((job['state'], job['outcome']), ('done', 'switched'))
        self.assertEqual(job['answer']['via'], 'window')
        self.assertIn('could not ask (the hub cannot be reached', self.log)
        self.assertIn('asking in the window alone', self.log)

    def test_a_window_answer_cancels_the_request(self):
        self.with_window()
        job = self.run_job({'block': True}, window=[{'decision': 'approve', 'message': 'here', 'after': 0.5}])
        self.assertEqual((job['outcome'], job['answer']['via']), ('switched', 'window'))
        [req] = self.asked()
        cancels = [c for c in self.calls_of('dither') if c[0] == 'cancel']
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0][1], req['id'])
        self.assertIn('answered in the window on laptop', cancels[0])
        self.assertLess(self.took, 20)

    def test_a_phone_answer_closes_the_window(self):
        self.with_window()
        job = self.run_job({'action': 'deny', 'message': 'no', 'after': 1}, window=[{'block': True}])
        self.assertEqual((job['outcome'], job['answer']['via'], job['message']), ('denied', 'dither', 'denied: no'))
        self.assertIn(['window-closed'], [json.loads(line) for line in self.calls.read_text().splitlines()])
        self.assertNotIn('cancel', [c[0] for c in self.calls_of('dither')])
        self.assertLess(self.took, 20)

    def test_a_failing_window_leaves_the_phone(self):
        self.with_window()
        job = self.run_job({'action': 'approve', 'after': 0.5}, window=[{'fail': True}])
        self.assertEqual((job['outcome'], job['answer']['via']), ('switched', 'dither'))
        self.assertIn('waiting on your devices alone', self.log)

    def test_a_change_while_asking_asks_again(self):
        job = self.run_job({'action': 'approve', 'touch': True}, {'action': 'approve'})
        self.assertEqual(job['outcome'], 'switched')
        first, second = self.asked()
        self.assertNotIn('checkout changed', first['body'])
        self.assertIn('The checkout changed while you were asked', second['body'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(len(self.calls_of('switch')), 1)
        self.assertEqual(len(self.calls_of('preview')), 2)

    def test_off_and_auto(self):
        config._loaded['ask']['dither'] = 'off'
        self.with_window()
        job = self.run_job(window=[{'decision': 'approve'}])
        self.assertEqual(job['outcome'], 'switched')
        self.assertEqual(self.asked(), [])
        config._loaded['ask'].update(dither='auto', dither_url=None)  # no DITHER_ASK_URL either
        job = self.run_job(window=[{'decision': 'approve'}])
        self.assertEqual(job['outcome'], 'switched')
        self.assertEqual(self.asked(), [])
        self.assertIn('no hub (DITHER_ASK_URL)', self.log)


class RunningCommitTest(Checkout):
    def test_the_running_commit(self):
        head = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], capture_output=True,
                              text=True).stdout.strip()
        self.assertEqual(ask.running_commit(self.repo, head), (head, None))
        self.assertEqual(ask.running_commit(self.repo, head[:12]), (head, None))
        rev, note = ask.running_commit(self.repo, f'{head[:12]}-dirty')
        self.assertEqual(rev, head)
        self.assertIn('with uncommitted changes', note)
        rev, note = ask.running_commit(self.repo, 'f' * 40)
        self.assertEqual(rev, head)
        self.assertIn('not in this checkout', note)

    def test_title_and_body_limits(self):
        self.assertEqual(ask.clean_title('a\tb\nc '), 'a b c')
        long = ask.clean_title('x' * 150 + '😀' * 60)
        self.assertIn(len(long.encode('utf-16-le')) // 2, (199, 200))  # no half of a surrogate pair
        self.assertTrue(long.endswith('…'))
        r = {'host': 'h', 'reason': 'r', 'uncommitted': [f'A\t{"p" * 200}{i}' for i in range(200)], 'commits': []}
        body = ask.body_text(r, None)
        self.assertLessEqual(len(body.encode()), ask.BODY_MAX)
        self.assertIn('cut to fit', body)

if __name__ == '__main__':
    unittest.main()
