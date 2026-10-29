import json
import unittest
from pathlib import Path

from update_manager import nixlog

DATA = Path(__file__).with_name('data')


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 1.0
        return self.t


class ProgressTest(unittest.TestCase):
    def feed(self):
        progress = nixlog.Progress(now=Clock())
        printed = [progress.feed(line) for line in (DATA / 'build.jsonl').read_text().splitlines(True)]
        return progress, [p for p in printed if p]

    def test_counts_plan_and_finished_builds(self):
        progress, printed = self.feed()
        snap = progress.snapshot()
        self.assertEqual(snap['builds']['expected'], 2)
        self.assertEqual(snap['builds']['done'], 2)
        self.assertEqual(progress.planned, ['um-test-a-1.0', 'um-test-b-2.0'])
        self.assertEqual([n for n, _ in progress.finished], ['um-test-a-1.0', 'um-test-b-2.0'])
        self.assertTrue(any(p.startswith('these 2 derivations will be built') for p in printed))
        self.assertTrue(any(p.startswith("building '/nix/store/") for p in printed))

    def test_plain_text_still_parses_the_plan(self):
        progress = nixlog.Progress()
        for line in ['these 3 derivations will be built:\n', '  /nix/store/' + 'a' * 32 + '-foo-1.0.drv\n',
                     'these 62 paths will be fetched (592.9 MiB download, 1.7 GiB unpacked):\n']:
            progress.feed(line)
        self.assertEqual(progress.planned, ['foo-1.0'])
        self.assertEqual(progress.planned_fetch['paths'], 62)
        self.assertAlmostEqual(progress.planned_fetch['download'], 592.9 * 2 ** 20)

    def test_garbage_is_ignored(self):
        progress = nixlog.Progress()
        self.assertIsNone(progress.feed('@nix {not json\n'))
        self.assertIsNone(progress.feed('@nix {"action":"result","id":9,"type":105,"fields":[1,2,3,4]}\n'))

    def test_history_and_eta(self):
        history = nixlog.merge_history({}, [('firefox-145.0', 600.0), ('hello-2.12', 4.0), ('tiny-1', 0.1)])
        self.assertEqual(history['names']['firefox-145.0'], 600.0)
        self.assertNotIn('tiny-1', history['names'])
        self.assertEqual(nixlog.typical(history, 'firefox-146.0'), 600.0)  # same package, new version
        snap = {'builds': {'done': 0, 'expected': 2, 'running': 1, 'failed': 0},
                'running': [{'name': 'firefox-146.0', 'elapsed': 100.0}], 'updated': 10.0, 'started': 0.0,
                'bytes': {'copied': 0, 'expected': 0}}
        eta = nixlog.eta(snap, ['firefox-146.0', 'hello-2.12'], [], history, jobs=1)
        self.assertEqual(eta['seconds'], 504)  # 500 left of firefox + 4 for hello, one at a time
        self.assertEqual(eta['quality'], 'history')

    def test_main_writes_progress_and_history(self):
        import io
        import sys
        import tempfile
        out = Path(tempfile.mkdtemp())
        from unittest import mock
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO((DATA / 'build.jsonl').read_text()), io.StringIO()
        try:  # A replayed log takes no time: let each reading of the clock be a second later.
            with mock.patch.object(nixlog.time, 'time', Clock()):
                nixlog.main(['--progress', str(out / 'p.json'), '--history', str(out / 'h.json')])
        finally:
            sys.stdin, sys.stdout = stdin, stdout
        snap = json.loads((out / 'p.json').read_text())
        self.assertEqual(snap['state'], 'finished')
        self.assertEqual(snap['builds']['done'], 2)
        self.assertIn('um-test-a-1.0', json.loads((out / 'h.json').read_text())['runs'])
