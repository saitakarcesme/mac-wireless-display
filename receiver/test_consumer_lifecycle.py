#!/usr/bin/env python3
"""A stuck old consumer cannot access a replacement session's player."""
import socket
import tempfile
import threading
import unittest

import time
from types import SimpleNamespace
from receiver import Receiver, HEADER, FrameWrite


class SyntheticReceiver(Receiver):
    def __init__(self, state, enabled=False):
        super().__init__(SimpleNamespace(state_dir=state, max_queue_ms=100))
        self.starts = 0

    def start_player(self):
        self.starts += 1
        self.player = SimpleNamespace(poll=lambda: None)

    def stop_player(self):
        self.player = None

    def write_frame(self, frame, grace):
        return FrameWrite(None, len(frame.data), len(frame.data), 0, 0, 0)


def send_frame(sock, seq, key=False):
    payload = b"\0\0\1\x65"
    sock.sendall(HEADER.pack(b"H264", 1, int(key), len(payload), seq,
                            time.monotonic_ns(), 0) + payload)


class ConsumerLifecycleTests(unittest.TestCase):
    def test_delayed_startup_refuses_takeover_until_consumer_ownership_ends(self):
        with tempfile.TemporaryDirectory() as state:
            receiver = SyntheticReceiver(state, enabled=False)
            entered, release = threading.Event(), threading.Event()
            original_start = receiver.start_player
            def delayed_start():
                entered.set()
                release.wait(5)
                original_start()
            receiver.start_player = delayed_start
            old, sender = socket.socketpair()
            old.settimeout(.05)
            receiver.active_connection = old
            errors = []
            def old_session():
                try:
                    receiver.session(old)
                except RuntimeError as exc:
                    errors.append(str(exc))
            session = threading.Thread(target=old_session)
            receiver.session_thread = session
            session.start()
            replacement, peer = socket.socketpair()
            try:
                send_frame(sender, 0, key=True)
                self.assertTrue(entered.wait(.5))
                sender.shutdown(socket.SHUT_RDWR)
                sender.close()
                session.join(3)
                self.assertFalse(session.is_alive())
                self.assertEqual(errors, ["Decoder consumer failed to stop within 2 seconds"])
                self.assertTrue(receiver.consumer_thread.is_alive())
                with self.assertRaisesRegex(RuntimeError, "replacement refused"):
                    receiver.replace_session(replacement)
                self.assertIsNone(receiver.active_connection)
                self.assertEqual(receiver.starts, 0)
                self.assertTrue(receiver.consumer_thread.is_alive())
                release.set()
                receiver.consumer_thread.join(1)
                self.assertFalse(receiver.consumer_thread.is_alive())
                self.assertEqual(receiver.starts, 1)
                # A later attempt may clean the old, now unowned player handle.
                receiver.retire_session()
                self.assertIsNone(receiver.consumer_thread)
                self.assertIsNone(receiver.player)
            finally:
                release.set()
                receiver.stop.set()
                sender.close()
                old.close()
                replacement.close()
                peer.close()
                session.join(1)
                if receiver.consumer_thread is not None:
                    receiver.consumer_thread.join(1)


if __name__ == '__main__':
    unittest.main()
