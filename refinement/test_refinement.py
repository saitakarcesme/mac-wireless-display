#!/usr/bin/env python3
"""Synthetic RGB only. No desktop capture, live mpv or system configuration."""
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock
import zlib

from refinement import (CleanEOF, MAX_HEADER, MAX_PAYLOAD, MAX_RAW, MpvOverlay,
                        ProtocolError, RefinementController, RefinementServer,
                        RendererError, decode_bgra, read_header, validate_header,
                        write_header)

W, H = 8, 4
RGB = bytes((5, 17, 253, 255, 255, 0, 64, 255)) * (W * H // 2)


def snapshot(epoch, sequence=10, raw=RGB):
    payload = zlib.compress(raw, 1)
    return {"version": 1, "kind": "snapshot", "epoch": epoch,
            "sequence": sequence, "width": W, "height": H, "stride": W * 4,
            "encoding": "bgra-zlib", "payload_bytes": len(payload)}, payload


class FakeRenderer:
    def __init__(self):
        self.calls = []
        self.pixels = None
        self.before_add = None
        self.before_remove = None
        self.fail_remove = False

    def add(self, path):
        if self.before_add:
            self.before_add()
        self.calls.append("add")
        self.mode = stat.S_IMODE(os.stat(path).st_mode)
        self.dir_mode = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
        self.pixels = Path(path).read_bytes()

    def remove(self):
        if self.before_remove:
            self.before_remove()
        self.calls.append("remove")
        if self.fail_remove:
            raise RendererError("synthetic ACK failure")
        self.pixels = None


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.renderer = FakeRenderer()
        self.control = RefinementController(self.renderer, W, H)
        self.epoch = self.control.begin_video()
        self.control.on_video_frame(10)

    def tearDown(self):
        self.renderer.fail_remove = False
        self.renderer.before_remove = None
        self.control.close()

    def test_exact_synthetic_rgb_and_private_temporary_files(self):
        self.assertEqual(self.control.apply(*snapshot(self.epoch)), (True, "applied"))
        self.assertEqual(self.renderer.pixels, RGB)
        self.assertEqual((self.renderer.mode, self.renderer.dir_mode), (0o600, 0o700))
        self.assertEqual(list(Path(self.control.tempdir.name).iterdir()), [])

    def test_snapshot_status_reports_actual_acked_image_then_motion_clear(self):
        initial = self.control.status_snapshot()
        self.assertFalse(initial["visible"])
        self.assertFalse(initial["unsafe"])
        self.assertIsNone(initial["snapshot_sequence"])
        self.assertEqual(initial["latest_sequence"], 10)
        self.control.apply(*snapshot(self.epoch))
        shown = self.control.status_snapshot()
        self.assertEqual(shown["mode"], "snapshot")
        self.assertTrue(shown["visible"])
        self.assertFalse(shown["unsafe"])
        self.assertEqual(shown["snapshot_sequence"], 10)
        self.assertEqual(shown["visible_tiles"], 1)
        self.assertEqual(shown["visible_mask"], "0x000000000001")
        self.control.on_video_frame(11)
        cleared = self.control.status_snapshot()
        self.assertEqual(cleared["latest_sequence"], 11)
        self.assertFalse(cleared["visible"])
        self.assertFalse(cleared["unsafe"])
        self.assertIsNone(cleared["snapshot_sequence"])

    def test_failed_add_status_never_claims_an_acked_snapshot(self):
        with mock.patch.object(self.renderer, "add", side_effect=RendererError("synthetic add ACK failure")):
            with self.assertRaises(RendererError):
                self.control.apply(*snapshot(self.epoch))
        uncertain = self.control.status_snapshot()
        self.assertTrue(uncertain["visible"])
        self.assertTrue(uncertain["unsafe"])
        self.assertIsNone(uncertain["snapshot_sequence"])
        self.assertEqual(uncertain["unsafe_tiles"], 1)
        self.control.on_video_frame(11)
        self.assertFalse(self.control.status_snapshot()["unsafe"])

    def test_motion_clears_once_and_steady_motion_does_no_rgb_work(self):
        self.control.apply(*snapshot(self.epoch))
        self.control.on_video_frame(11)
        self.assertIsNone(self.renderer.pixels)
        calls = list(self.renderer.calls)
        with mock.patch("refinement.decode_bgra", side_effect=AssertionError("pixel work")):
            with mock.patch("refinement.tempfile.mkstemp", side_effect=AssertionError("copy")):
                for sequence in range(12, 1000):
                    self.control.on_video_frame(sequence)
        self.assertEqual(self.renderer.calls, calls)

    def test_stale_seq_or_epoch_does_not_decompress(self):
        self.control.on_video_frame(11)
        with mock.patch("refinement.decode_bgra", side_effect=AssertionError("pixel work")):
            self.assertFalse(self.control.apply(*snapshot(self.epoch))[0])
            self.assertFalse(self.control.apply(*snapshot(str(uuid.uuid4()), 11))[0])

    def test_same_sequence_new_session_cannot_reuse_old_snapshot(self):
        old = snapshot(self.epoch)
        self.control.begin_video()
        self.control.on_video_frame(10)
        self.assertFalse(self.control.apply(*old)[0])

    def test_remove_ack_is_a_barrier_and_sequence_publishes_before_wait(self):
        self.control.apply(*snapshot(self.epoch))
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        self.renderer.before_remove = lambda: (entered.set(), release.wait(2))
        worker = threading.Thread(target=lambda: (self.control.on_video_frame(11), completed.set()))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.control.active_generation(), (self.epoch, 11))
        self.assertFalse(completed.is_set())
        release.set()
        worker.join(1)
        self.assertTrue(completed.is_set())
        self.assertIsNone(self.renderer.pixels)

    def test_motion_during_add_never_leaves_old_overlay_over_new_frame(self):
        entered, release = threading.Event(), threading.Event()
        self.renderer.before_add = lambda: (entered.set(), release.wait(2))
        result = []
        refinement = threading.Thread(target=lambda: result.append(self.control.apply(*snapshot(self.epoch))))
        refinement.start()
        self.assertTrue(entered.wait(1))
        motion_done = threading.Event()
        motion = threading.Thread(target=lambda: (self.control.on_video_frame(11), motion_done.set()))
        motion.start()
        deadline = time.monotonic() + 1
        while self.control.active_generation()[1] != 11 and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertEqual(self.control.active_generation()[1], 11)
        self.assertFalse(motion_done.is_set())
        release.set()
        refinement.join(1)
        motion.join(1)
        self.assertEqual(result, [(False, "motion-during-display")])
        self.assertTrue(motion_done.is_set())
        self.assertIsNone(self.renderer.pixels)

    def test_remove_failure_stays_unsafe_until_successful_retry(self):
        self.control.apply(*snapshot(self.epoch))
        self.renderer.fail_remove = True
        with self.assertRaises(RendererError):
            self.control.on_video_frame(11)
        self.assertTrue(self.control.visible)
        failed = self.control.status_snapshot()
        self.assertTrue(failed["unsafe"])
        self.assertEqual(failed["snapshot_sequence"], 10)
        self.assertEqual(failed["latest_sequence"], 11)
        self.renderer.fail_remove = False
        self.control.on_video_frame(12)
        self.assertFalse(self.control.visible)
        cleared = self.control.status_snapshot()
        self.assertFalse(cleared["unsafe"])
        self.assertIsNone(cleared["snapshot_sequence"])

    def test_disconnect_and_session_end_clear_overlay(self):
        left, right = socket.socketpair()
        with left, right:
            epoch = self.control.attach_client(left)
            self.control.apply(*snapshot(self.epoch))
            self.control.detach_client(left, epoch)
            self.assertIsNone(self.renderer.pixels)
        self.control.apply(*snapshot(self.epoch))
        self.control.end_video()
        self.assertEqual(self.control.active_generation(), (None, None))
        self.assertIsNone(self.renderer.pixels)

    def test_sequence_must_increase_and_wrong_geometry_rejected(self):
        for value in (10, 9, -1, True, 1.5, 1 << 64):
            with self.assertRaises(ProtocolError):
                self.control.on_video_frame(value)
        for field, value in (("width", 0), ("height", 1081), ("stride", 1),
                             ("payload_bytes", MAX_PAYLOAD + 1), ("sequence", True),
                             ("version", True), ("encoding", "png"), ("epoch", "x")):
            header, _ = snapshot(self.epoch)
            header[field] = value
            with self.assertRaises(ProtocolError, msg=field):
                validate_header(header, W, H)


class ProtocolTests(unittest.TestCase):
    def test_clean_eof_is_distinct_from_truncated_prefix(self):
        for prefix in (b"", b"\0", b"\0\0\0"):
            left, right = socket.socketpair()
            with left, right:
                if prefix:
                    left.sendall(prefix)
                left.shutdown(socket.SHUT_WR)
                with self.assertRaises(EOFError) as caught:
                    read_header(right)
                self.assertEqual(isinstance(caught.exception, CleanEOF), not bool(prefix))

    def test_one_shot_clean_eof_retains_until_motion_or_epoch_end(self):
        renderer = FakeRenderer()
        control = RefinementController(renderer, W, H)
        control.begin_video()
        control.on_video_frame(10)
        server = RefinementServer(control, port=0)
        server.start()
        def send_once(sequence):
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:
                epoch = read_header(client)["epoch"]
                header, payload = snapshot(epoch, sequence)
                write_header(client, header)
                client.sendall(payload)
                self.assertTrue(read_header(client)["accepted"])
            deadline = time.monotonic() + 1
            while control.clients and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertFalse(control.clients)
        try:
            send_once(10)
            self.assertEqual(renderer.pixels, RGB)
            self.assertIsNone(server.last_error)
            retained = control.status_snapshot()
            self.assertTrue(retained["visible"])
            self.assertFalse(retained["unsafe"])
            self.assertEqual(retained["snapshot_sequence"], 10)
            control.on_video_frame(11)
            self.assertIsNone(renderer.pixels)
            send_once(11)
            self.assertEqual(renderer.pixels, RGB)
            control.end_video()
            self.assertIsNone(renderer.pixels)
            self.assertIsNone(server.last_error)
            self.assertFalse(control.status_snapshot()["visible"])
            self.assertIsNone(control.status_snapshot()["snapshot_sequence"])
        finally:
            server.stop()
            control.close()

    def test_partial_message_after_retained_snapshot_clears_and_reports_error(self):
        renderer = FakeRenderer()
        control = RefinementController(renderer, W, H)
        epoch = control.begin_video()
        control.on_video_frame(10)
        control.apply(*snapshot(epoch))
        server = RefinementServer(control, port=0)
        server.start()
        try:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:
                read_header(client)
                client.sendall(b"\0")
            deadline = time.monotonic() + 1
            while server.last_error is None and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertTrue(server.last_error.startswith("EOFError:"))
            # Error is recorded before the finally clear; synchronize on detach.
            while control.clients and time.monotonic() < deadline:
                time.sleep(.001)
            with control.render_lock:
                self.assertIsNone(renderer.pixels)
            self.assertFalse(control.status_snapshot()["visible"])
            self.assertIsNone(control.status_snapshot()["snapshot_sequence"])
            # A fresh accepted image resolves the old serialized error. Clean
            # EOF still retains this image until the authoritative video moves.
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:
                hello = read_header(client)
                header, payload = snapshot(hello["epoch"])
                write_header(client, header)
                client.sendall(payload)
                self.assertTrue(read_header(client)["accepted"])
            deadline = time.monotonic() + 1
            while control.clients and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertFalse(control.clients)
            self.assertIsNone(server.last_error)
            recovered = control.status_snapshot()
            self.assertTrue(recovered["visible"])
            self.assertFalse(recovered["unsafe"])
            self.assertEqual(recovered["snapshot_sequence"], 10)
        finally:
            server.stop()
            control.close()

    def test_maximum_native_synthetic_snapshot_byte_integrity(self):
        raw = bytes((7, 129, 251, 255)) * (1920 * 1080)
        self.assertEqual(len(raw), MAX_RAW)
        self.assertEqual(decode_bgra(zlib.compress(raw, 1), MAX_RAW), raw)

    def test_zlib_exact_bytes_and_reject_truncated_checksum_trailing_and_bomb(self):
        data = zlib.compress(RGB)
        self.assertEqual(decode_bgra(data, len(RGB)), RGB)
        invalid = [data[:-1], data + b"extra", data + data, data[:-1] + bytes([data[-1] ^ 1]),
                   zlib.compress(RGB + b"\0"), zlib.compress(b"x" * (1024 * 1024)),
                   zlib.compress(RGB[:-1]), zlib.compress(bytes(len(RGB)))]
        for payload in invalid:
            with self.assertRaises(ProtocolError):
                decode_bgra(payload, len(RGB))
        with self.assertRaises(ProtocolError):
            decode_bgra(data, MAX_RAW + 1)

    def test_framed_json_bounds_and_duplicate_keys(self):
        for payload in (b'[]', b'{"version":1,"version":2}', b'\xff'):
            left, right = socket.socketpair()
            with left, right:
                left.sendall(struct.pack(">I", len(payload)) + payload)
                with self.assertRaises(ProtocolError):
                    read_header(right)
        for length in (0, MAX_HEADER + 1, 2**32 - 1):
            left, right = socket.socketpair()
            with left, right:
                left.sendall(struct.pack(">I", length))
                with self.assertRaises(ProtocolError):
                    read_header(right)

    def test_loopback_handshake_apply_idle_disconnect_and_new_epoch(self):
        renderer = FakeRenderer()
        control = RefinementController(renderer, W, H)
        control.begin_video()
        control.on_video_frame(10)
        server = RefinementServer(control, port=0, io_timeout=.1)
        self.assertEqual(server.socket.getsockname()[0], "127.0.0.1")
        server.start()
        try:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:
                hello = read_header(client)
                header, payload = snapshot(hello["epoch"])
                write_header(client, header)
                client.sendall(payload)
                self.assertTrue(read_header(client)["accepted"])
                # Empty idle sessions must not lose the static picture on I/O timeout.
                time.sleep(.2)
                self.assertEqual(renderer.pixels, RGB)
                old_epoch = hello["epoch"]
                control.begin_video()
                self.assertEqual(client.recv(1), b"")
                self.assertIsNone(renderer.pixels)
            control.on_video_frame(10)
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:
                self.assertNotEqual(read_header(client)["epoch"], old_epoch)
        finally:
            server.stop()
            control.close()

    def test_server_stop_interrupts_partial_payload(self):
        renderer = FakeRenderer()
        control = RefinementController(renderer, W, H)
        control.begin_video()
        control.on_video_frame(10)
        server = RefinementServer(control, port=0)
        server.start()
        try:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:
                epoch = read_header(client)["epoch"]
                header, _ = snapshot(epoch)
                write_header(client, header)
                client.sendall(b"x")
                started = time.monotonic()
                server.stop()
                self.assertLess(time.monotonic() - started, 1)
        finally:
            control.close()


class MpvAdapterTests(unittest.TestCase):
    def test_native_geometry_and_one_id_atomic_removal(self):
        adapter = MpvOverlay("unused", W, H)
        calls = []
        def command(args):
            calls.append(args)
            return {"w": W, "h": H, "ml": 0, "mr": 0, "mt": 0, "mb": 0}
        adapter.command = command
        adapter.add("/synthetic.bgra")
        adapter.remove()
        self.assertEqual(calls[-1], ["overlay-remove", 0])
        self.assertEqual(calls[1], {"name": "overlay-add", "id": 0, "x": 0, "y": 0,
                                   "file": "/synthetic.bgra", "offset": 0, "fmt": "bgra",
                                   "w": W, "h": H, "stride": W * 4, "dw": W, "dh": H})
        adapter.command = lambda args: {"w": W, "h": H, "ml": 1}
        with self.assertRaises(RendererError):
            adapter.add("/synthetic.bgra")

    def test_real_ipc_framing_and_ack_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "mpv.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(1)
            received = []
            def fake_mpv():
                for result in ("success", "invalid parameter"):
                    client, _ = listener.accept()
                    with client:
                        data = bytearray()
                        while not data.endswith(b"\n"):
                            data.extend(client.recv(4096))
                        received.append(json.loads(data))
                        client.sendall(b'{"event":"idle"}\n' + json.dumps(
                            {"request_id": 1, "error": result}).encode() + b"\n")
            worker = threading.Thread(target=fake_mpv)
            worker.start()
            adapter = MpvOverlay(path, W, H)
            adapter.remove()
            with self.assertRaises(RendererError):
                adapter.remove()
            worker.join(1)
            listener.close()
            self.assertEqual(len(received), 2)


if __name__ == "__main__":
    unittest.main()
