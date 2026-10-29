import unittest

from update_manager import profiles


class ProfilesTest(unittest.TestCase):
    def test_locked_params(self):
        p = profiles.locked_params('path:/x?lastModified=1792172353&narHash=sha256-XT%3D')
        self.assertEqual(p, {'narHash': 'sha256-XT=', 'lastModified': 1792172353})
        g = profiles.locked_params('github:o/r/' + 'e' * 40 + '?narHash=sha256-x')
        self.assertEqual(g['rev'], 'e' * 40)

    def test_is_newer(self):
        self.assertTrue(profiles.is_newer({'rev': 'a'}, {'rev': 'b'}))
        self.assertFalse(profiles.is_newer({'narHash': 'h'}, {'narHash': 'h', 'rev': None}))
        self.assertTrue(profiles.is_newer({'narHash': 'h'}, {'narHash': 'i'}))

    def test_newest_tag(self):
        text = '\n'.join(f'{"0" * 40}\trefs/tags/{t}' for t in
                         ['v0.1.5', 'v0.1.5-z3.1', 'v0.1.5-z3.10', 'v0.1.5-z3.2', 'v0.1.4-z3.9', 'v0.1.6-nightly.1'])
        self.assertEqual(profiles.newest_tag(text, 'v*-z3.*'), 'v0.1.5-z3.10')
        self.assertIsNone(profiles.newest_tag('', 'v*'))
