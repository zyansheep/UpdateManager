import json
import tempfile
import unittest
from pathlib import Path

from update_manager import system


def infos(*names, size=10_000):
    return {f'/nix/store/{"a" * 32}-{n}': {'narSize': size} for n in names}


class SystemTest(unittest.TestCase):
    def test_diff_closures(self):
        old = infos('linux-6.18.48', 'firefox-144.0', 'bash-5.3', 'gone-1.0', 'etc')
        new = infos('linux-6.18.55', 'firefox-145.0', 'bash-5.3', 'vnstat-2.13', 'etc')
        new[f'/nix/store/{"a" * 32}-etc']['narSize'] = 50_000
        diff = system.diff_closures(old, new)
        names = [c['name'] for c in diff['changes']]
        self.assertEqual(set(names[:2]), {'linux', 'firefox'})  # notable version changes first
        self.assertEqual(diff['upgraded'], 2)
        self.assertEqual(diff['added'], 1)
        self.assertEqual(diff['removed'], 1)
        self.assertIn('etc', names)  # size change only
        self.assertNotIn('bash', names)
        self.assertEqual(diff['kernel'], {'from': '6.18.48', 'to': '6.18.55'})

    def test_split_name(self):
        self.assertEqual(system.split_name('python3.11-yt-dlp-2024.12.6'), ('python3.11-yt-dlp', '2024.12.6'))
        self.assertEqual(system.split_name('etc'), ('etc', ''))
        self.assertEqual(system.kernel_version('/nix/store/' + 'k' * 32 + '-linux-6.18.55/bzImage'), '6.18.55')

    def test_dry_run(self):
        out = system.parse_dry_run('these 30 derivations will be built:\n  /nix/store/x.drv\n'
                                   'these 62 paths will be fetched (592.9 MiB download, 1.7 GiB unpacked):\n')
        self.assertEqual(out['builds'], 30)
        self.assertEqual(out['paths'], 62)
        self.assertAlmostEqual(out['unpacked'], 1.7 * 2 ** 30)
        self.assertEqual(system.parse_dry_run('', 0)['download'], 0)
        self.assertIn('error', system.parse_dry_run('error: boom', 1))

    def test_missing_estimate(self):
        target = infos('a-1', 'b-1', size=100)
        have = {next(iter(target))}
        est = system.missing_estimate(target, have)
        self.assertEqual((est['paths'], est['download']), (1, 100))

    def test_time_estimate(self):
        settings = {'estimates': {'download_bps': 10e6, 'activation_s': 30}}
        self.assertEqual(system.time_estimate(100e6, settings, history=[])['seconds'], 40)
        history = [{'bytes': 200e6, 'download_s': 10, 'activation_s': 20}]
        est = system.time_estimate(100e6, settings, history=history)
        self.assertEqual((est['seconds'], est['measured']), (25, True))

    def test_flake_inputs(self):
        lock = {'root': 'root', 'version': 7, 'nodes': {
            'root': {'inputs': {'nixpkgs': 'nixpkgs_2', 'stable': ['nixpkgs'], 'pinned': 'p'}},
            'nixpkgs_2': {'locked': {'type': 'github', 'owner': 'nixos', 'repo': 'nixpkgs', 'rev': 'f' * 40,
                                     'lastModified': 1000}, 'original': {'ref': 'nixos-26.05'}},
            'p': {'locked': {'type': 'github', 'owner': 'o', 'repo': 'r', 'rev': 'e' * 40, 'lastModified': 1000},
                  'original': {'rev': 'e' * 40}}}}
        path = Path(tempfile.mkdtemp()) / 'flake.lock'
        path.write_text(json.dumps(lock))
        items = system.flake_inputs(path, now=1000 + 86400 * 3)
        self.assertEqual([i['name'] for i in items], ['nixpkgs', 'pinned'])
        self.assertEqual(items[0]['age_days'], 3.0)
        self.assertEqual(items[0]['ref'], 'nixos-26.05')
        self.assertTrue(items[1]['pinned'])

    def test_update_section_current_and_light(self):
        ci_state = {'main': {'rev': 'b' * 40, 'built': 1, 'hosts': {'laptop': {'ok': True, 'path': '/nix/store/'
                                                                               + 'c' * 32 + '-nixos-system-laptop-1'}}}}
        current = system.update_section('laptop', {'current': '/nix/store/' + 'c' * 32 + '-nixos-system-laptop-1'},
                                        ci_state, settings={'flake': '/nonexistent', 'nixos_ci': {'state': '/x'}})
        self.assertEqual(current['state'], 'current')
        light = system.update_section('laptop', {'current': '/nix/store/' + 'd' * 32 + '-nixos-system-laptop-0'},
                                      ci_state, settings={'flake': '/nonexistent', 'nixos_ci': {'state': '/x'}},
                                      heavy=False)
        self.assertEqual(light['state'], 'local')  # its commit isn't known: not an update, a local build
        self.assertTrue(light['pending'])
        self.assertIsNone(light['behind'])
        dirty = system.update_section('laptop', {'current': '/nix/store/' + 'd' * 32 + '-nixos-system-laptop-0',
                                                 'rev': 'abcdef123456-dirty'},
                                      ci_state, settings={'flake': '/nonexistent', 'nixos_ci': {'state': '/x'}},
                                      heavy=False)
        self.assertEqual(dirty['state'], 'local')
