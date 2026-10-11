#!/usr/bin/env python3
"""Protocol/queue tests without mpv or display."""
import time
import os
import socket
import threading
from types import SimpleNamespace
import unittest
from receiver import Frame, FrameQueue, FrameWrite, HEADER, Receiver


def frame(seq, key=False, age=0):
    return Frame(seq, 100, key, b"\x00\x00\x00\x01\x65", time.monotonic() - age)


def write_result(f, reason=None, partial=0):
    return FrameWrite(reason, partial if reason else len(f.data), len(f.data), 1, 0, 1)


class QueueTests(unittest.TestCase):
    def test_header_matches_fixed_network_layout(self):
        self.assertEqual(HEADER.size, 32)
        data = HEADER.pack(b"H264", 1, 1, 3000, 123, 456, 0)
        self.assertEqual(data[:12], b"H264\x00\x01\x00\x01\x00\x00\x0b\xb8")
        self.assertEqual(HEADER.unpack(data), (b"H264", 1, 1, 3000, 123, 456, 0))

    def test_waits_for_idr_then_preserves_references(self):
        q = FrameQueue(.1)
        q.put(frame(1))
        q.put(frame(2, key=True))
        q.put(frame(3))
        self.assertEqual(q.get()[0].sequence, 2)
        self.assertEqual(q.get()[0].sequence, 3)
        self.assertEqual(q.dropped, 1)

    def test_overflow_waits_for_new_keyframe(self):
        q = FrameQueue(.1)
        q.put(frame(1, key=True, age=.2))
        q.put(frame(2))
        q.put(frame(3))
        self.assertEqual(len(q.frames), 0)
        self.assertTrue(q.need_key)
        q.put(frame(4, key=True))
        next_frame, reset = q.get()
        self.assertEqual(next_frame.sequence, 4)
        self.assertTrue(reset)

    def test_late_consumer_discards_reference_chain(self):
        q = FrameQueue(.1)
        q.put(frame(1, key=True, age=.2))
        q.put(frame(2, age=.19))
        next_frame, reset = q.get()
        self.assertIsNone(next_frame)
        self.assertTrue(reset)
        self.assertTrue(q.need_key)
        self.assertEqual(q.dropped, 2)

    def test_startup_grace_applies_to_entire_queue(self):
        q = FrameQueue(.1)
        q.warmup()
        q.put(frame(1, key=True, age=.2))
        q.put(frame(2))
        self.assertEqual(q.get()[0].sequence, 1)
        self.assertEqual(q.get()[0].sequence, 2)

    def test_discontinuity_invalidates_stream(self):
        q = FrameQueue(.1)
        q.put(frame(1, key=True))
        q.invalidate()
        q.put(frame(4))
        q.put(frame(5, key=True))
        next_frame, reset = q.get()
        self.assertEqual(next_frame.sequence, 5)
        self.assertTrue(reset)

    def test_capacity_bound_still_applies_during_startup(self):
        q = FrameQueue(.1)
        q.max_bytes = 10
        q.warmup()
        q.put(frame(1, key=True))
        q.put(frame(2))
        q.put(frame(3))
        self.assertEqual(q.snapshot()["queue_bytes"], 0)
        self.assertTrue(q.need_key)
        self.assertEqual(q.last_reset_reason, "queue-capacity")


class FakeReceiver(Receiver):
    """Exercises lifecycle and recovery without spawning a decoder or GUI."""
    def __init__(self):
        self.stop = threading.Event()
        self.stats_lock = threading.Lock()
        self.stats = {"written_frames": 0, "dropped_frames": 0}
        self.player = None
        self.starts = 0
        self.stops = 0
        self.written = []
        self.active_connection = None
        self.session_thread = None

    def start_player(self):
        self.starts += 1
        self.player = object()

    def stop_player(self):
        self.stops += 1
        self.player = None


class RecoveryTests(unittest.TestCase):
    def test_real_nonblocking_pipe_records_partial_bytes_and_bounded_age(self):
        receiver = FakeReceiver()
        receiver.args = SimpleNamespace(max_queue_ms=15)
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        output = os.fdopen(write_fd, "wb", buffering=0)
        receiver.player = SimpleNamespace(stdin=output, poll=lambda: None)
        try:
            f = Frame(10, 100, True, b"a" * (4 * 1024 * 1024), time.monotonic())
            result = receiver.write_frame(f, 0)
            self.assertEqual(result.reason, "pipe-write-deadline")
            self.assertGreater(result.bytes_written, 0)
            self.assertLess(result.bytes_written, result.bytes_total)
            self.assertTrue(result.decoder_broken)
            self.assertGreaterEqual(result.age_at_end_ms, 15)
            self.assertLess(result.age_at_end_ms, 100)
        finally:
            output.close()
            os.close(read_fd)

    def test_real_full_pipe_timeout_records_zero_bytes_without_decoder_corruption(self):
        receiver = FakeReceiver()
        receiver.args = SimpleNamespace(max_queue_ms=15)
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        output = os.fdopen(write_fd, "wb", buffering=0)
        receiver.player = SimpleNamespace(stdin=output, poll=lambda: None)
        try:
            while True:
                try:
                    os.write(write_fd, b"a" * 65536)
                except BlockingIOError:
                    break
            result = receiver.write_frame(frame(10, key=True), 0)
            self.assertEqual(result.reason, "pipe-write-deadline")
            self.assertEqual(result.bytes_written, 0)
            self.assertFalse(result.decoder_broken)
            self.assertLess(result.age_at_end_ms, 100)
        finally:
            output.close()
            os.close(read_fd)

    def test_complete_au_gap_recovers_at_idr_without_reopening_window(self):
        receiver = FakeReceiver()
        q = FrameQueue(.1)
        q.put(frame(1, key=True))

        def write(f, grace):
            receiver.written.append(f.sequence)
            if f.sequence == 1:
                q.invalidate("queue-age")
                q.put(frame(2))  # Cannot be decoded: reference missing.
                q.put(frame(3, key=True))
                q.put(frame(4))
                q.close()
            return write_result(f)

        receiver.write_frame = write
        receiver.consume(q, 0)
        self.assertEqual(receiver.written, [1, 3, 4])
        self.assertEqual(receiver.starts, 1)
        self.assertEqual(receiver.stops, 0)
        self.assertEqual(receiver.stats["last_recovery_reason"], "queue-age")

    def test_partial_au_write_failure_restarts_decoder(self):
        receiver = FakeReceiver()
        q = FrameQueue(.1)
        q.put(frame(1, key=True))
        q.put(frame(2))

        def write(f, grace):
            receiver.written.append(f.sequence)
            return write_result(f, "pipe-write-deadline", partial=2) if f.sequence == 2 else write_result(f)

        def stop():
            receiver.stops += 1
            receiver.player = None
            q.put(frame(3, key=True))
            q.close()

        receiver.write_frame = write
        receiver.stop_player = stop
        receiver.consume(q, 0)
        self.assertEqual(receiver.written, [1, 2, 3])
        self.assertEqual(receiver.starts, 2)
        self.assertEqual(receiver.stops, 1)
        self.assertEqual(receiver.stats["last_decoder_restart_reason"], "pipe-write-deadline")
        self.assertEqual(receiver.stats["last_failed_frame_write"]["bytes_written"], 2)

    def test_zero_byte_deadline_keeps_decoder_and_waits_for_new_idr(self):
        receiver = FakeReceiver()
        q = FrameQueue(.1)
        q.put(frame(1, key=True))
        q.put(frame(2))
        original_invalidate = q.invalidate

        def invalidate(reason, extra_dropped=0):
            original_invalidate(reason, extra_dropped)
            q.put(frame(3))
            q.put(frame(4, key=True))
            q.close()

        def write(f, grace):
            receiver.written.append(f.sequence)
            return write_result(f, "pipe-write-deadline") if f.sequence == 2 else write_result(f)

        q.invalidate = invalidate
        receiver.write_frame = write
        receiver.consume(q, 0)
        self.assertEqual(receiver.written, [1, 2, 4])
        self.assertEqual(receiver.starts, 1)
        self.assertEqual(receiver.stops, 0)
        self.assertEqual(receiver.stats["dropped_frames"], 2)
        self.assertEqual(receiver.stats["last_failed_frame_write"]["bytes_written"], 0)
        self.assertEqual(receiver.stats["write_failure_reasons"], {"pipe-write-deadline": 1})

    def test_broken_pipe_requires_restart_even_without_written_bytes(self):
        self.assertTrue(write_result(frame(1), "decoder-pipe-closed").decoder_broken)

    def test_new_connection_takes_over_stale_session_without_waiting_for_eof(self):
        receiver = FakeReceiver()
        order = []
        entered = threading.Event()

        def session(connection):
            order.append("start")
            entered.set()
            try:
                while connection.recv(1):
                    pass
            except OSError:
                pass
            order.append("end")

        receiver.session = session
        first, peer1 = socket.socketpair()
        second, peer2 = socket.socketpair()
        try:
            receiver.replace_session(first)
            self.assertTrue(entered.wait(1))
            entered.clear()
            start = time.monotonic()
            receiver.replace_session(second)
            self.assertLess(time.monotonic() - start, 1)
            self.assertTrue(entered.wait(1))
            self.assertEqual(order, ["start", "end", "start"])
            receiver.retire_session()
            self.assertEqual(order[-1], "end")
        finally:
            peer1.close()
            peer2.close()
            receiver.retire_session()


if __name__ == "__main__":
    unittest.main()
