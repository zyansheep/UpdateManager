import os
import tempfile
import unittest
from pathlib import Path

from update_manager import ci

DATA = Path(__file__).with_name('data')
STATUS = """rev     61b4c721496ee4e87aabbf5ad11c607ec5767333
built   2026-10-27T23:16:37-07:00 in 1 min
isomorph ok /nix/store/6q38xyrc67g4afpq5ic280c1f172dm4f-nixos-system-isomorph-26.11.20261010.6510408
functor FAILED
"""


class CiTest(unittest.TestCase):
    def test_status(self):
        s = ci.parse_status(STATUS)
        self.assertEqual(s['rev'][:12], '61b4c721496e')
        self.assertEqual(s['minutes'], 1)
        self.assertTrue(s['hosts']['isomorph']['ok'])
        self.assertFalse(s['hosts']['functor']['ok'])
        self.assertFalse(s['lock'])
        self.assertTrue(ci.parse_status(STATUS + 'lock    nixos-ci lock\n')['lock'])

    def test_show(self):
        units = ci.parse_show('Id=a.service\nActiveState=activating\nSubState=start\n'
                              'ExecMainStartTimestamp=@1793302200\n\nId=b.timer\nNextElapseUSecRealtime=@5\n')
        self.assertEqual(units['a.service']['since'], 1793302200.0)
        self.assertEqual(units['b.timer']['next'], 5.0)

    def test_history_from_journal(self):
        runs = ci.history([], journal=(DATA / 'journal.jsonl').read_text())
        self.assertTrue(runs)
        self.assertGreaterEqual(runs[0]['start'], runs[-1]['start'])
        for r in runs:
            self.assertRegex(r['rev12'], r'^[0-9a-f]{12}$')
            if r['end']:
                self.assertGreater(r['seconds'], 0)
        self.assertIsNotNone(ci.typical_seconds(runs))

    def test_roots_target_and_rev(self):
        state = Path(tempfile.mkdtemp())
        host = state / 'roots/main/laptop'
        host.mkdir(parents=True)
        os.symlink('/nix/store/' + 'a' * 32 + '-nixos-system-laptop-1', host / '20261027-211620-10ca1067f840')
        os.symlink('/nix/store/' + 'b' * 32 + '-nixos-system-laptop-1', host / '20261027-231637-61b4c721496e')
        roots = ci.roots(state)
        self.assertEqual([r['rev12'] for r in roots['laptop']], ['61b4c721496e', '10ca1067f840'])
        ci_state = {'roots': roots, 'main': ci.parse_status(STATUS)}
        self.assertEqual(ci.rev_of('/nix/store/' + 'a' * 32 + '-nixos-system-laptop-1', ci_state), '10ca1067f840')
        self.assertEqual(ci.target_for('laptop', ci_state)['rev'], '61b4c721496e')  # from the roots
        self.assertIsNone(ci.target_for('functor', {'main': ci.parse_status(STATUS)}))  # it failed

    def test_running_eta_from_typical(self):
        info = ci.running({'nixos-ci.service': {'active': 'activating', 'sub': 'start', 'since': 100.0}},
                          [{'rev12': 'abc', 'subject': 's', 'start': 101.0}], Path('/nonexistent'), 160.0, 300)
        self.assertEqual(info['phase'], 'building')
        self.assertEqual(info['eta_s'], 240)
        self.assertIsNone(ci.running({'nixos-ci.service': {'active': 'inactive'}}, [], Path('/x'), 1, None))
