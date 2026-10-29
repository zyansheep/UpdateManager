import time
import unittest

from update_manager import waybar


def state(**summary):
    base = {'behind': [], 'reboot': [], 'running': [], 'ci_running': False, 'ci': None, 'profile_updates': 0,
            'forks_behind': [], 'offline': []}
    base.update(summary)
    hosts = {'functor': {'update': {'behind': 3, 'download': {'download': 0}}, 'jobs': []},
             'isomorph': {'update': {'behind': 3, 'download': {'download': 24 * 2 ** 20}}, 'jobs': []}}
    return {'updated': time.time(), 'summary': base, 'hosts': hosts}


class WaybarTest(unittest.TestCase):
    def test_current(self):
        out = waybar.label(state())
        self.assertEqual(out['text'], waybar.GLYPHS['current'])
        self.assertIn('current', out['class'])

    def test_updates(self):
        out = waybar.label(state(behind=['functor', 'isomorph'], reboot=['functor']))
        self.assertEqual(out['text'], f"{waybar.GLYPHS['updates']} 2")
        self.assertIn('reboot', out['class'])
        self.assertIn('isomorph: 3 commits behind · 24 MiB', out['tooltip'])

    def test_building_with_progress(self):
        ci = {'phase': 'building', 'rev12': 'abc', 'eta_s': 600,
              'progress': {'builds': {'done': 1, 'expected': 4}}}
        out = waybar.label(state(ci_running=True, ci=ci))
        self.assertEqual(out['text'], f"{waybar.GLYPHS['building']} 25%")
        self.assertIn('10 min left', out['tooltip'])

    def test_switching_wins(self):
        running = [{'host': 'isomorph', 'kind': 'switch', 'title': 'Switch x',
                    'progress': {'builds': {}, 'bytes': {'copied': 50, 'expected': 100}}}]
        out = waybar.label(state(running=running, ci_running=True, ci={}))
        self.assertEqual(out['text'], f"{waybar.GLYPHS['switching']} 50%")

    def test_stale_and_empty(self):
        old = state()
        old['updated'] = time.time() - 3600
        self.assertIn('stale', waybar.label(old)['class'])
        self.assertIn('unknown', waybar.label(None)['class'])
