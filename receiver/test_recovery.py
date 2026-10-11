#!/usr/bin/env python3
"""Synthetic recovery tests. No decoder, desktop, or network listener is used."""
import collections
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import recovery
from receiver import Frame, FrameQueue, FrameWrite, HEADER, Receiver
from recovery import (ACCEPT, CONTROL, DECLINE, HELLO, IDR_SENT, NONE_SEQUENCE,
                      REQUEST_IDR, RecoveryControl, pack, receive_header, unpack)


def wait_for(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Condition timed out")
        time.sleep(.002)


def read_record(sock):
    data = bytearray()
    while len(data) < CONTROL.size:
        part = sock.recv(CONTROL.size - len(data))
        if not part:
            raise EOFError()
        data.extend(part)
    return unpack(bytes(data))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.local, self.host = socket.socketpair()
        self.local.settimeout(.1)
        self.host.settimeout(.4)
        self.control = RecoveryControl(self.local, enabled=True)

    def tearDown(self):
        self.control.close()
        self.local.close()
        self.host.close()

    def negotiate(self):
        self.control.on_control(pack(HELLO, 17))
        self.assertEqual(read_record(self.host), (ACCEPT, 17, 0, NONE_SEQUENCE))
        wait_for(lambda: self.control.negotiated)

    def request(self, seq=10):
        self.control.on_video(seq, False)
        self.control.request("test-loss")
        record = read_record(self.host)
        self.assertEqual(record[0:2], (REQUEST_IDR, 17))
        self.assertEqual(record[3], seq)
        return record[2]

    def test_fixed_layout_and_invalid_records(self):
        self.assertEqual(CONTROL.size, 32)
        valid = pack(HELLO, 17)
        self.assertEqual(valid[:8], b"HCTL\x01\x01\x00\x00")
        cases = [b"", valid[:-1], pack(HELLO, 0), pack(HELLO, 17, 1),
                 pack(HELLO, 17, sequence=0), pack(REQUEST_IDR, 17),
                 pack(IDR_SENT, 17), CONTROL.pack(b"HCTL", 2, HELLO, 0, 17, 0, NONE_SEQUENCE),
                 CONTROL.pack(b"HCTL", 1, HELLO, 1, 17, 0, NONE_SEQUENCE),
                 CONTROL.pack(b"HCTL", 1, 99, 0, 17, 0, NONE_SEQUENCE)]
        for record in cases:
            with self.subTest(record=record), self.assertRaises(ValueError):
                unpack(record)

    def test_silent_until_hello_even_when_recovery_needed(self):
        self.control.request("missing-initial-idr")
        self.host.settimeout(.03)
        with self.assertRaises(socket.timeout):
            self.host.recv(1)
        self.assertFalse(self.control.negotiated)

    def test_disabled_receiver_declines_without_requesting(self):
        self.control.enabled = False
        self.control.request("test-loss")
        self.control.on_control(pack(HELLO, 17))
        self.assertEqual(read_record(self.host), (DECLINE, 17, 0, NONE_SEQUENCE))
        self.host.settimeout(.03)
        with self.assertRaises(socket.timeout):
            self.host.recv(1)
        self.assertFalse(self.control.negotiated)

    def test_hello_after_video_or_duplicate_is_rejected(self):
        self.control.on_video(0, True)
        with self.assertRaises(ValueError):
            self.control.on_control(pack(HELLO, 17))
        self.control.last_sequence = None
        self.negotiate()
        with self.assertRaises(ValueError):
            self.control.on_control(pack(HELLO, 18))

    def test_ack_does_not_clear_recovery_and_old_idr_cannot_clear_new_loss(self):
        self.negotiate()
        request_id = self.request()
        self.control.on_video(11, True)
        self.control.on_control(pack(IDR_SENT, 17, request_id, 11))
        self.assertTrue(self.control.pending)
        self.control.on_video(12, False)
        self.control.request("new-loss-while-old-idr-is-in-flight")
        self.control.on_idr_written(11)
        self.assertTrue(self.control.pending)
        self.control.on_video(13, True)
        self.control.on_idr_written(13)
        self.assertFalse(self.control.pending)

    def test_ack_may_arrive_after_successful_idr_write(self):
        self.negotiate()
        request_id = self.request()
        self.control.on_video(11, True)
        self.control.on_idr_written(11)
        self.control.on_control(pack(IDR_SENT, 17, request_id, 11))
        self.assertEqual(self.control.last_ack_id, request_id)
        self.assertFalse(self.control.pending)

    def test_ack_rejects_epoch_unknown_duplicate_future_and_non_idr(self):
        self.negotiate()
        request_id = self.request()
        self.control.on_video(11, True)
        for record in (pack(IDR_SENT, 18, request_id, 11),
                       pack(IDR_SENT, 17, request_id + 1, 11),
                       pack(IDR_SENT, 17, request_id, 12),
                       pack(IDR_SENT, 17, request_id, 10),
                       pack(IDR_SENT, 17, request_id, NONE_SEQUENCE),
                       pack(REQUEST_IDR, 17, request_id, 11)):
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.control.on_control(record)
        self.control.on_control(pack(IDR_SENT, 17, request_id, 11))
        with self.assertRaises(ValueError):
            self.control.on_control(pack(IDR_SENT, 17, request_id, 11))

    def test_no_initial_frame_uses_sentinel(self):
        self.negotiate()
        self.control.request("missing-initial-idr")
        self.assertEqual(read_record(self.host), (REQUEST_IDR, 17, 1, NONE_SEQUENCE))

    def test_repeated_loss_is_coalesced_and_retry_is_rate_limited(self):
        self.negotiate()
        self.request()
        for _ in range(1000):
            self.control.request("test-loss")
        self.host.settimeout(.05)
        with self.assertRaises(socket.timeout):
            self.host.recv(1)
        with self.control.condition:
            self.control.last_send -= 1.1
            self.control.condition.notify_all()
        self.host.settimeout(.4)
        self.assertEqual(read_record(self.host)[2], 2)
        self.assertEqual(len(self.control.requests), 2)
        self.assertEqual(self.control.coalesced_requests, 1000)

    def test_new_recovery_waits_at_least_250ms_after_previous_request(self):
        self.negotiate()
        self.request()
        wait_for(lambda: self.control.sent_requests == 1)
        self.control.on_video(11, True)
        self.control.on_idr_written(11)
        self.control.on_video(12, False)
        self.control.request("new-loss")
        started = time.monotonic()
        self.assertEqual(read_record(self.host)[2], 2)
        self.assertGreaterEqual(time.monotonic() - started, .22)

    def test_malformed_flood_is_bounded(self):
        self.negotiate()
        for _ in range(15):
            with self.assertRaises(ValueError):
                self.control.on_control(pack(ACCEPT, 17))
        with self.assertRaisesRegex(ValueError, "rate exceeded"):
            self.control.on_control(pack(ACCEPT, 17))
        self.assertLessEqual(len(self.control.records), 16)

    def test_reverse_write_failure_closes_stream_without_retrying_partial_record(self):
        def fail(data, deadline):
            self.control.socket.send(data[:9])
            raise TimeoutError("simulated partial reverse write")
        self.control._send = fail
        self.control.on_control(pack(HELLO, 17))
        wait_for(lambda: self.control.error is not None)
        self.assertFalse(self.control.negotiated)
        self.assertEqual(self.host.recv(100), pack(ACCEPT, 17)[:9])
        self.assertEqual(self.host.recv(1), b"")

    def test_request_and_idr_history_stay_bounded(self):
        self.negotiate()
        with patch.object(recovery, "RETRY_INTERVAL", .001):
            self.control.request("persistent-loss")
            for request_id in range(1, 41):
                self.assertEqual(read_record(self.host)[2], request_id)
                self.assertLessEqual(len(self.control.requests), 16)
        for sequence in range(100):
            self.control.on_video(sequence, True)
        self.assertEqual(len(self.control.idrs), 64)
        self.assertEqual(self.control.idrs[0], 36)

    def test_accept_is_not_effective_until_complete_reverse_write(self):
        entered, release = threading.Event(), threading.Event()
        original_send = self.control._send
        def gated_send(data, deadline):
            entered.set()
            release.wait(.3)
            original_send(data, deadline)
        self.control._send = gated_send
        self.control.on_control(pack(HELLO, 17))
        self.control.request("initial-loss")
        self.assertTrue(entered.wait(.2))
        self.assertFalse(self.control.negotiated)
        self.assertEqual(self.control.sent_requests, 0)
        release.set()
        self.assertEqual(read_record(self.host)[0], ACCEPT)
        self.assertEqual(read_record(self.host)[0], REQUEST_IDR)

    def test_new_connection_has_no_request_or_epoch_state_from_previous(self):
        self.negotiate()
        self.request()
        local, peer = socket.socketpair()
        peer.settimeout(.3)
        fresh = RecoveryControl(local, enabled=True)
        try:
            self.assertIsNone(fresh.epoch)
            self.assertFalse(fresh.pending)
            self.assertFalse(fresh.negotiated)
            fresh.on_control(pack(HELLO, 99))
            self.assertEqual(read_record(peer)[1], 99)
            wait_for(lambda: fresh.negotiated)
            with self.assertRaises(ValueError):
                fresh.on_control(pack(IDR_SENT, 17, 1, 11))
            fresh.request("fresh-loss")
            self.assertEqual(read_record(peer), (REQUEST_IDR, 99, 1, NONE_SEQUENCE))
        finally:
            fresh.close()
            local.close()
            peer.close()

    def test_closed_original_descriptor_does_not_retarget_reverse_writer(self):
        self.local.close()
        unrelated, peer = socket.socketpair()
        try:
            peer.settimeout(.03)
            self.control.on_control(pack(HELLO, 17))
            self.assertEqual(read_record(self.host)[0], ACCEPT)
            with self.assertRaises(socket.timeout):
                peer.recv(1)
        finally:
            unrelated.close()
            peer.close()

    def test_stalled_reverse_socket_has_bounded_deadline(self):
        while True:
            try:
                self.control.socket.send(b"x" * 65536, socket.MSG_DONTWAIT)
            except BlockingIOError:
                break
        started = time.monotonic()
        self.control.on_control(pack(HELLO, 17))
        wait_for(lambda: self.control.error is not None)
        self.assertIn("deadline", self.control.error)
        self.assertLess(time.monotonic() - started, .6)
        self.assertFalse(self.control.negotiated)


class HeaderTests(unittest.TestCase):
    def test_idle_allowed_but_partial_header_deadline_applies(self):
        local, peer = socket.socketpair()
        local.settimeout(.02)
        stop = threading.Event()
        output = []
        def read():
            try:
                output.append(receive_header(local, stop))
            except Exception as exc:
                output.append(exc)
        worker = threading.Thread(target=read)
        worker.start()
        try:
            time.sleep(.04)
            self.assertTrue(worker.is_alive())
            peer.sendall(b"H")
            worker.join(.6)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(output[0], TimeoutError)
        finally:
            stop.set()
            local.close()
            peer.close()
            worker.join(1)

    def test_annex_b_idr_check_accepts_three_and_four_byte_start_codes(self):
        self.assertTrue(recovery.contains_idr(b"\0\0\1\x65payload"))
        self.assertTrue(recovery.contains_idr(b"\0\0\0\1\x67sps\0\0\1\x65slice"))
        self.assertFalse(recovery.contains_idr(b"\0\0\1\x41delta"))
        self.assertFalse(recovery.contains_idr(b"\0\0\1"))

    def test_fragmented_header_recovers_exactly_one_record(self):
        local, peer = socket.socketpair()
        local.settimeout(.02)
        record = pack(HELLO, 17)
        try:
            peer.sendall(record[:3])
            worker = threading.Thread(target=lambda: (time.sleep(.005), peer.sendall(record[3:])))
            worker.start()
            self.assertEqual(receive_header(local, threading.Event()), record)
            worker.join()
        finally:
            local.close()
            peer.close()


class SyntheticReceiver(Receiver):
    def __init__(self, path, enabled=True):
        super().__init__(SimpleNamespace(state_dir=path, max_queue_ms=100,
                                         recovery_control=enabled))
        self.written = []
        self.starts = 0
        self.fail_sequence = None

    def start_player(self):
        self.starts += 1
        self.player = SimpleNamespace(exited=False)
        self.player.poll = lambda: 0 if self.player.exited else None

    def stop_player(self):
        self.player = None

    def write_frame(self, frame, grace):
        if frame.sequence == self.fail_sequence:
            return FrameWrite("pipe-write-deadline", 2, len(frame.data), 100, 0, 100)
        self.written.append(frame.sequence)
        return FrameWrite(None, len(frame.data), len(frame.data), 0, 0, 0)


def send_frame(sock, seq, key=False):
    payload = b"\0\0\0\1" + (b"\x65" if key else b"\x41") + b"synthetic"
    sock.sendall(HEADER.pack(b"H264", 1, int(key), len(payload), seq, time.monotonic_ns(), 0) + payload)


class SessionTests(unittest.TestCase):
    def test_idle_source_recovery_after_sequence_gap_and_partial_pipe_failure(self):
        for partial in (False, True):
            with self.subTest(partial=partial), tempfile.TemporaryDirectory() as path:
                receiver = SyntheticReceiver(path)
                receiver.fail_sequence = 1 if partial else None
                local, host = socket.socketpair()
                local.settimeout(.05)
                host.settimeout(.5)
                worker = threading.Thread(target=receiver.session, args=(local,))
                worker.start()
                try:
                    host.sendall(pack(HELLO, 17))
                    self.assertEqual(read_record(host)[0], ACCEPT)
                    send_frame(host, 0, key=True)
                    wait_for(lambda: 0 in receiver.written)
                    send_frame(host, 1 if partial else 2)
                    opcode, epoch, request_id, sequence = read_record(host)
                    self.assertEqual(opcode, REQUEST_IDR)
                    self.assertEqual(sequence, 1 if partial else 2)
                    # No animation or delta frames follow. A mock host sends a
                    # fresh IDR only in response to the reverse request.
                    send_frame(host, 3, key=True)
                    host.sendall(pack(IDR_SENT, epoch, request_id, 3))
                    wait_for(lambda: 3 in receiver.written)
                    self.assertFalse(receiver.recovery.pending)
                    self.assertEqual(receiver.starts, 2 if partial else 1)
                    self.assertNotIn(1 if partial else 2, receiver.written)
                finally:
                    host.shutdown(socket.SHUT_RDWR)
                    host.close()
                    worker.join(2)
                    local.close()
                self.assertFalse(worker.is_alive())
                self.assertIsNone(receiver.recovery.error)

    def test_idle_decoder_exit_requests_idr_without_another_video_frame(self):
        with tempfile.TemporaryDirectory() as path:
            receiver = SyntheticReceiver(path)
            local, host = socket.socketpair()
            local.settimeout(.05)
            host.settimeout(.6)
            worker = threading.Thread(target=receiver.session, args=(local,))
            worker.start()
            try:
                host.sendall(pack(HELLO, 17))
                self.assertEqual(read_record(host)[0], ACCEPT)
                send_frame(host, 0, key=True)
                wait_for(lambda: 0 in receiver.written)
                receiver.player.exited = True
                opcode, epoch, request_id, sequence = read_record(host)
                self.assertEqual(opcode, REQUEST_IDR)
                self.assertEqual(sequence, 0)
                send_frame(host, 1, key=True)
                host.sendall(pack(IDR_SENT, epoch, request_id, 1))
                wait_for(lambda: 1 in receiver.written)
                self.assertEqual(receiver.starts, 2)
                self.assertEqual(receiver.stats["last_decoder_restart_reason"], "decoder-exited-idle")
                self.assertFalse(receiver.recovery.pending)
            finally:
                host.shutdown(socket.SHUT_RDWR)
                host.close()
                worker.join(2)
                local.close()
            self.assertFalse(worker.is_alive())

    def test_legacy_stream_never_receives_unsolicited_control(self):
        with tempfile.TemporaryDirectory() as path:
            receiver = SyntheticReceiver(path, enabled=True)
            local, host = socket.socketpair()
            local.settimeout(.05)
            host.settimeout(.03)
            worker = threading.Thread(target=receiver.session, args=(local,))
            worker.start()
            try:
                send_frame(host, 0, key=True)
                wait_for(lambda: 0 in receiver.written)
                send_frame(host, 2)
                with self.assertRaises(socket.timeout):
                    host.recv(1)
                send_frame(host, 3, key=True)
                wait_for(lambda: 3 in receiver.written)
                self.assertEqual(receiver.written, [0, 3])
            finally:
                host.shutdown(socket.SHUT_RDWR)
                host.close()
                worker.join(2)
                local.close()

    def test_queue_coalesces_missing_initial_idr_and_preserves_bound(self):
        reasons = []
        queue = FrameQueue(.1, reasons.append)
        now = time.monotonic()
        for seq in range(1000):
            queue.put(Frame(seq, 0, False, b"x", now))
        self.assertEqual(reasons, ["missing-initial-idr"])
        self.assertEqual(queue.snapshot()["queue_bytes"], 0)
        self.assertEqual(queue.dropped, 1000)


if __name__ == "__main__":
    unittest.main()
