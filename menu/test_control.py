"""Offline lifecycle checks. Never reach the real receiver or user config."""
import importlib.util
import errno
import sys
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

spec = importlib.util.spec_from_file_location("h264_control", Path(__file__).with_name("h264-control.py"))
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class ControlTests(unittest.TestCase):
    def test_preflight_does_not_connect_to_occupied_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(0.03)
            self.assertFalse(control.port_available(listener.getsockname()[1]))
            with self.assertRaises(socket.timeout):
                listener.accept()

    def test_free_port_is_available(self):
        self.assertTrue(control.port_available(0))

    def test_recently_closed_forward_is_available_despite_time_wait(self):
        # Model OpenSSH's reusable listener and active server-side close. The
        # closing handshake is observed explicitly; no fixed sleep is needed.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
                accepted, _ = listener.accept()
                with accepted:
                    accepted.settimeout(1)
                    listener.close()
                    accepted.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b"")
                    client.shutdown(socket.SHUT_WR)
                    self.assertEqual(accepted.recv(1), b"")
        with socket.socket() as old_probe:
            with self.assertRaises(OSError) as caught:
                old_probe.bind(("127.0.0.1", port))
            self.assertEqual(caught.exception.errno, errno.EADDRINUSE)
        self.assertTrue(control.port_available(port))

    def test_reusable_active_listeners_still_block_without_accepting_connections(self):
        for address in ("0.0.0.0", "127.0.0.1"):
            for reuseport in (False, True):
                with self.subTest(address=address, reuseport=reuseport), socket.socket() as listener:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if reuseport and hasattr(socket, "SO_REUSEPORT"):
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    listener.bind((address, 0))
                    listener.listen(1)
                    listener.settimeout(.01)
                    self.assertFalse(control.port_available(listener.getsockname()[1]))
                    with self.assertRaises(socket.timeout):
                        listener.accept()

    def test_socket_option_failure_is_closed(self):
        with patch.object(control.socket, "socket") as create:
            create.return_value.__enter__.return_value.setsockopt.side_effect = OSError("generated failure")
            self.assertFalse(control.port_available(19001))

    def test_parent_eof_requests_cleanup(self):
        session = control.Session()
        with patch.object(control.sys, "stdin", []):
            session.read_parent()
        self.assertTrue(session.stop.is_set())

    def test_stale_host_pid_is_never_killed(self):
        session = control.Session()
        session.host = (12345, ("expected executable", "expected start"))
        with patch.object(control, "process_identity", return_value=("other executable", "other start")), \
             patch.object(control.os, "kill") as kill:
            session.stop_host()
        kill.assert_not_called()

    def test_disconnect_stops_only_owned_tunnel_and_receiver(self):
        session = control.Session()
        session.target = "display-receiver"
        session.receiver_owned = True
        session.tunnel = MagicMock()
        with patch.object(session, "command") as command:
            session.cleanup()
        command.assert_called_once_with("systemctl --user stop mac-h264-display.service", timeout=5)
        session.tunnel.terminate.assert_called_once()
        session.tunnel.wait.assert_called_once_with(timeout=2)

    def test_receiver_cleanup_failure_still_stops_owned_tunnel(self):
        session = control.Session()
        session.target = "display-receiver"
        session.receiver_owned = True
        session.tunnel = MagicMock()
        with patch.object(session, "command", side_effect=subprocess.TimeoutExpired("ssh", 5)):
            session.cleanup()
        session.tunnel.terminate.assert_called_once()

    def test_no_claim_means_receiver_is_not_stopped(self):
        session = control.Session()
        session.target = "display-receiver"
        with patch.object(session, "command") as command:
            session.cleanup()
        command.assert_not_called()

    def test_ssh_uses_user_alias_and_requires_verified_identity(self):
        session = control.Session(base=Path("/isolated/test"))
        session.target = "display-receiver"
        arguments = session.ssh()
        self.assertNotIn("-i", arguments)
        self.assertFalse(any(x.startswith("UserKnownHostsFile=") for x in arguments))
        self.assertIn("StrictHostKeyChecking=yes", arguments)
        self.assertEqual(arguments[-1], "display-receiver")
        self.assertNotIn("tailscale", " ".join(arguments))


if __name__ == "__main__":
    unittest.main()
