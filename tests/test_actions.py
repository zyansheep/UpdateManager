import json
import unittest

from update_manager import collect, config, jobs, remote

SYSTEM = '/nix/store/' + 'a' * 32 + '-nixos-system-laptop-26.11'


class ActionsTest(unittest.TestCase):
    def test_menu_validates(self):
        self.assertEqual(jobs.validate('switch', {'path': SYSTEM})[0], 'switch')
        for bad in ({'path': '/etc/passwd'}, {'path': SYSTEM + ' ; rm -rf /'},
                    {'path': '/nix/store/' + 'a' * 32 + '-hello-1.0'}, {}):
            with self.assertRaises(jobs.ActionError):
                jobs.validate('switch', bad)
        with self.assertRaises(jobs.ActionError):
            jobs.validate('rm', {})
        with self.assertRaises(jobs.ActionError):
            jobs.validate('profile-upgrade', {'name': 'x; reboot'})
        self.assertEqual(jobs.validate('rebuild', {'pull': 1, 'extra': 'ignored'})[2], {'pull': True})

    def test_switch_refuses_another_hosts_system(self):
        job = {'id': 'x', 'kind': 'switch'}
        with self.assertRaises(jobs.ActionError):
            jobs.do_switch(job, {'path': '/nix/store/' + 'a' * 32 + '-nixos-system-other-1'}, None)

    def test_job_runs_and_records(self):
        job = jobs.start('refresh', {}, wait=True)
        self.assertEqual(job['state'], 'done')
        self.assertTrue((config.RUNTIME / 'refresh').exists())
        self.assertEqual(jobs.recent(1)[0]['id'], job['id'])

    def test_outbox(self):
        remote.queue('laptop2', 'switch', {'path': SYSTEM})
        remote.queue('laptop2', 'rebuild', {})  # replaces the switch: one at a time
        remote.queue('laptop2', 'profile-upgrade', {'name': 't3'})
        self.assertEqual([r['action'] for r in remote.pending('laptop2')], ['rebuild', 'profile-upgrade'])
        reply = remote.serve_exchange('laptop2', json.dumps({'host': 'laptop2', 'self': {'host': 'laptop2'}}),
                                      lambda: {'host': 'laptop'})
        self.assertEqual(len(reply['requests']), 2)
        self.assertEqual(remote.pending('laptop2'), [])
        self.assertEqual(remote.peer_state('laptop2')['state']['host'], 'laptop2')
        with self.assertRaises(jobs.ActionError):
            remote.queue('laptop2', 'rm', {})

    def test_shell_command_quotes(self):
        cmd = remote.shell_command(['do', 'switch', '{"path": "x y"}'],
                                   settings={'remote': {'command': ['/run/current-system/sw/bin/update-manager',
                                                                    '~/.nix-profile/bin/update-manager']}})
        self.assertIn("exec /run/current-system/sw/bin/update-manager do switch '{\"path\": \"x y\"}'", cmd)
        self.assertIn('$HOME/.nix-profile/bin/update-manager', cmd)
        self.assertTrue(cmd.endswith('exit 127'))

    def test_view_and_summary(self):
        own = {'host': 'laptop', 'self': {'host': 'laptop', 'update': {'state': 'available'}, 'jobs': [],
                                          'system': {'reboot_pending': True}},
               'ci': {'running': None}}
        peers = {'desk': {'received': 1.0, 'state': {'self': {'host': 'desk', 'update': {'state': 'current'}}},
                          'source': 'pulled'}}
        view = collect.view(own, peers, now=100.0, settings={'hosts': {'desk': {}, 'laptop': {}, 'third': {}},
                                                             'ci_host': 'desk'})
        self.assertEqual(list(view['hosts']), ['desk', 'laptop', 'third'])
        self.assertEqual(view['hosts']['third']['source'], 'none')
        self.assertEqual(view['summary']['behind'], ['laptop'])
        self.assertEqual(view['summary']['reboot'], ['laptop'])
        self.assertIn('third', view['summary']['offline'])
