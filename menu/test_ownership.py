"""Execute the actual remote ownership code with isolated state and fake systemd."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess
from test_control import control


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.state = self.home / '.local/state/mac-h264-display'
        self.state.mkdir(parents=True)
        self.active = True
        self.invocation = 'old-invocation'
        self.calls = []
        self.namespace = {'__name__': 'test_remote'}
        exec(control.REMOTE_SESSION, self.namespace)

    def tearDown(self):
        self.temp.cleanup()

    def run_systemctl(self, argv, **kwargs):
        args = argv[2:]; self.calls.append(args)
        if args[0] == 'show': return CompletedProcess(argv, 0, self.invocation, '')
        if args[0] == 'is-active': return CompletedProcess(argv, 0 if self.active else 3, '', '')
        if args[0] == 'stop': self.active = False
        if args[0] == 'start': self.active = True; self.invocation = 'new-invocation'
        return CompletedProcess(argv, 0, '', '')

    def manage(self, action, client='client-a', session='session-new'):
        with patch.object(Path, 'home', return_value=self.home), \
             patch.object(control.subprocess, 'run', side_effect=self.run_systemctl):
            self.namespace['manage'](action, client, session)

    def owner(self, client='client-a', session='old-session', invocation='old-invocation'):
        (self.state / 'menu-owner.json').write_text(json.dumps(dict(client=client, session=session, invocation=invocation)))

    def test_reclaim_own_connected_session_after_outage(self):
        self.owner(); (self.state / 'status.json').write_text('{"connected":true}')
        self.manage('claim')
        self.assertIn(['stop', 'mac-h264-display.service'], self.calls)
        self.assertEqual(json.loads((self.state / 'menu-owner.json').read_text())['session'], 'session-new')

    def test_foreign_connected_session_is_never_stopped(self):
        self.owner(client='client-b'); (self.state / 'status.json').write_text('{"connected":true}')
        with self.assertRaises(RuntimeError): self.manage('claim')
        self.assertFalse(any(x[0] in ('start', 'stop') for x in self.calls))

    def test_changed_invocation_is_not_claimed_from_stale_owner(self):
        self.owner(invocation='earlier-invocation'); (self.state / 'status.json').write_text('{"connected":true}')
        with self.assertRaises(RuntimeError): self.manage('claim')
        self.assertFalse(any(x[0] == 'stop' for x in self.calls))

    def test_old_cleanup_cannot_stop_new_session(self):
        self.owner(session='newer-session')
        self.manage('release', session='old-session')
        self.assertFalse(any(x[0] == 'stop' for x in self.calls))

    def test_release_exact_owned_session(self):
        self.owner(); self.manage('release', session='old-session')
        self.assertFalse(self.active)
        self.assertFalse((self.state / 'menu-owner.json').exists())

    def test_unverifiable_active_receiver_is_preserved(self):
        with self.assertRaises(RuntimeError): self.manage('claim')
        self.assertFalse(any(x[0] == 'stop' for x in self.calls))

    def test_idle_unowned_receiver_can_be_claimed(self):
        (self.state / 'status.json').write_text('{"connected":false}')
        self.manage('claim')
        self.assertTrue((self.state / 'menu-owner.json').exists())

    def test_client_id_persists_and_is_private(self):
        first = control.installation_id(self.home)
        self.assertEqual(first, control.installation_id(self.home))
        self.assertEqual((self.home / 'client-id').stat().st_mode & 0o777, 0o600)

if __name__ == '__main__': unittest.main()
