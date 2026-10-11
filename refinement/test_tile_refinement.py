"""Generated RGB + fake IPC only; no desktop, live player or installation."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
import zlib

from refinement import (ALL_TILES, MAX_TILE_PAYLOAD, MpvOverlay, ProtocolError,
                        RefinementServer, RendererError, TILE_COUNT, TILE_HEIGHT,
                        TILE_WIDTH, TileRefinementController, read_header,
                        validate_tile_header, write_header)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
from receiver import DIRTY_MASK, HEADER, Receiver

PIXELS = bytes((7, 101, 253, 255)) * (TILE_WIDTH * TILE_HEIGHT)


def tile_message(epoch, tile=0, sequence=10):
    payload = zlib.compress(PIXELS, 1)
    return {"version": 2, "kind": "tile", "epoch": epoch, "sequence": sequence,
            "tile": tile, "width": 240, "height": 180, "stride": 960,
            "encoding": "bgra-zlib", "payload_bytes": len(payload)}, payload


class Renderer:
    def __init__(self):
        self.tiles = {}
        self.removed = []
        self.before_add = None
        self.before_remove = None
        self.fail_remove = False

    def command(self, command):
        return 123

    def add_tile(self, path, tile):
        if self.before_add:
            self.before_add()
        self.tiles[tile] = Path(path).read_bytes()

    def remove_tiles(self, mask):
        self.removed.append(mask)
        if self.before_remove:
            self.before_remove()
        if self.fail_remove:
            raise RendererError("synthetic missing ACK")
        for tile in range(TILE_COUNT):
            if mask & (1 << tile):
                self.tiles.pop(tile, None)


class TileTests(unittest.TestCase):
    def setUp(self):
        self.renderer = Renderer()
        self.control = TileRefinementController(self.renderer)
        self.epoch = self.control.begin_video()
        self.control.on_video_frame(10, ALL_TILES)

    def tearDown(self):
        self.renderer.fail_remove = False
        self.renderer.before_remove = None
        self.control.close()

    def apply(self, tile=0, seq=10):
        return self.control.apply(*tile_message(self.epoch, tile, seq))

    def test_unrelated_motion_retains_rgb_and_accepts_older_unchanged_tile(self):
        self.assertTrue(self.apply(0)[0])
        self.control.on_video_frame(11, 1 << 1)
        self.assertEqual(self.renderer.tiles[0], PIXELS)
        self.assertTrue(self.apply(2, 10)[0])
        self.assertEqual(self.control.visible_tiles, (1 << 0) | (1 << 2))

    def test_changed_tile_rejected_before_decode_then_new_generation_accepted(self):
        self.apply(0)
        self.apply(1)
        self.control.on_video_frame(11, 1 << 0)
        self.assertNotIn(0, self.renderer.tiles)
        self.assertIn(1, self.renderer.tiles)
        with mock.patch("refinement.decode_bgra", side_effect=AssertionError("decoded stale tile")):
            self.assertFalse(self.apply(0, 10)[0])
        self.assertTrue(self.apply(0, 11)[0])

    def test_missing_mask_and_first_epoch_frame_are_conservative(self):
        self.apply(0)
        self.apply(47)
        self.control.on_video_frame(11)
        self.assertEqual(self.renderer.tiles, {})
        epoch = self.control.begin_video()
        self.control.on_video_frame(100, 0)
        self.assertFalse(self.control.apply(*tile_message(epoch, 0, 99))[0])
        self.assertTrue(self.control.apply(*tile_message(epoch, 0, 100))[0])

    def test_future_sequence_and_old_epoch_never_apply(self):
        self.assertFalse(self.apply(0, 11)[0])
        self.assertFalse(self.control.apply(*tile_message(str(uuid.uuid4())))[0])

    def test_accumulated_mask_over_sequence_gap_removes_every_affected_tile(self):
        for tile in (0, 1, 2):
            self.apply(tile)
        self.control.on_video_frame(15, (1 << 0) | (1 << 2))
        self.assertEqual(set(self.renderer.tiles), {1})
        self.assertEqual(self.renderer.removed[-1], (1 << 0) | (1 << 2))

    def test_zero_dirty_motion_does_no_rgb_work_or_renderer_call(self):
        self.apply(0)
        before = list(self.renderer.removed)
        with mock.patch("refinement.decode_bgra", side_effect=AssertionError("pixels")):
            with mock.patch("refinement.tempfile.mkstemp", side_effect=AssertionError("file")):
                for seq in range(11, 1000):
                    self.control.on_video_frame(seq, 0)
        self.assertEqual(self.renderer.removed, before)
        self.assertEqual(self.renderer.tiles[0], PIXELS)

    def test_dirty_generation_publishes_before_batched_ack_barrier(self):
        self.apply(0)
        self.apply(47)
        entered, release, complete = threading.Event(), threading.Event(), threading.Event()
        self.renderer.before_remove = lambda: (entered.set(), release.wait(1))
        worker = threading.Thread(target=lambda: (self.control.on_video_frame(11, 1 | (1 << 47)), complete.set()))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.control.last_dirty[0], 11)
        self.assertEqual(self.control.last_dirty[47], 11)
        self.assertFalse(complete.is_set())
        release.set()
        worker.join(1)
        self.assertTrue(complete.is_set())
        self.assertFalse(self.renderer.tiles)

    def test_same_tile_motion_during_add_clears_before_forwarding(self):
        entered, release = threading.Event(), threading.Event()
        self.renderer.before_add = lambda: (entered.set(), release.wait(1))
        result = []
        adding = threading.Thread(target=lambda: result.append(self.apply(0)))
        adding.start()
        self.assertTrue(entered.wait(1))
        moved = threading.Event()
        motion = threading.Thread(target=lambda: (self.control.on_video_frame(11, 1), moved.set()))
        motion.start()
        deadline = time.monotonic() + 1
        while self.control.active_generation()[1] != 11 and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertFalse(moved.is_set())
        release.set()
        adding.join(1)
        motion.join(1)
        self.assertEqual(result, [(False, "tile-dirtied-during-display")])
        self.assertFalse(self.renderer.tiles)

    def test_failed_removal_keeps_unsafe_ids_for_retry(self):
        self.apply(0)
        self.renderer.fail_remove = True
        with self.assertRaises(RendererError):
            self.control.on_video_frame(11, 1)
        self.assertTrue(self.control.visible_tiles & 1)
        self.renderer.fail_remove = False
        # Even an unrelated/empty next mask must retry any unacknowledged IDs.
        self.control.on_video_frame(12, 0)
        self.assertFalse(self.control.visible_tiles)
        self.assertFalse(self.control.unsafe_tiles)

    def test_bounds_reject_coordinates_dimensions_payload_and_mask(self):
        for field, bad in [("tile", 48), ("tile", -1), ("tile", True), ("width", 1920),
                           ("height", 0), ("stride", 961), ("payload_bytes", MAX_TILE_PAYLOAD + 1)]:
            header, _ = tile_message(self.epoch)
            header[field] = bad
            with self.assertRaises(ProtocolError):
                validate_tile_header(header)
        for mask in (-1, 1 << 48, True):
            with self.assertRaises(ProtocolError):
                self.control.on_video_frame(11, mask)

    def test_clean_eof_after_stale_tile_retains_other_confirmed_tiles(self):
        server = RefinementServer(self.control, port=0)
        server.start()
        try:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:
                hello = read_header(client)
                self.assertEqual((hello["version"], hello["columns"], hello["rows"]), (2, 8, 6))
                for tile, seq, expected in [(0, 10, True), (1, 9, False)]:
                    header, payload = tile_message(hello["epoch"], tile, seq)
                    write_header(client, header)
                    client.sendall(payload)
                    reply = read_header(client)
                    self.assertEqual((reply["version"], reply["tile"], reply["accepted"]), (2, tile, expected))
            deadline = time.monotonic() + 1
            while self.control.clients and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertIn(0, self.renderer.tiles)
            self.assertIsNone(server.last_error)
        finally:
            server.stop()


class BatchTests(unittest.TestCase):
    def test_all48_tile_coordinates_are_native_and_bounded(self):
        renderer = MpvOverlay("unused")
        calls = []
        def command(args):
            calls.append(args)
            return {"w": 1920, "h": 1080}
        renderer.command = command
        for tile in range(48):
            renderer.add_tile("/generated.bgra", tile)
            command = calls[-1]
            self.assertEqual((command["x"], command["y"]), ((tile % 8) * 240, (tile // 8) * 180))
            self.assertLessEqual(command["x"] + command["w"], 1920)
            self.assertLessEqual(command["y"] + command["h"], 1080)

    def test_one_connection_batch_waits_for_all48_acknowledgements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "ipc")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(1)
            received, remaining, complete = [], threading.Event(), threading.Event()
            def mpv():
                client, _ = listener.accept()
                with client:
                    file = client.makefile("rb")
                    for _ in range(48):
                        received.append(json.loads(file.readline()))
                    for request in reversed(received[1:]):
                        client.sendall(json.dumps({"request_id": request["request_id"], "error": "success"}).encode() + b"\n")
                    remaining.set()
                    time.sleep(.03)
                    client.sendall(b'{"request_id":1,"error":"success"}\n')
            server = threading.Thread(target=mpv)
            server.start()
            worker = threading.Thread(target=lambda: (MpvOverlay(path).remove_tiles(ALL_TILES), complete.set()))
            worker.start()
            self.assertTrue(remaining.wait(1))
            self.assertFalse(complete.is_set())
            worker.join(1)
            server.join(1)
            listener.close()
            self.assertTrue(complete.is_set())
            self.assertEqual(len(received), 48)
            self.assertEqual(received[-1]["command"], ["overlay-remove", 47])


class HeaderReceiver(Receiver):
    def __init__(self, state_dir):
        super().__init__(SimpleNamespace(state_dir=state_dir, max_queue_ms=100,
                                         measure_presentation=False, refinement_port=0))
        self.forwarded = []
        self.admitted = threading.Event()

    def consume(self, queue, _):
        while True:
            frame, _ = queue.get()
            if frame:
                self.forwarded.append((frame.sequence, frame.data, self.refinement.visible_tiles))
                self.admitted.set()
            if queue.closed:
                return

    def stop_player(self):
        self.end_refinement()
        self.player = None


class TileReceiverTests(unittest.TestCase):
    def test_v1_video_invalidates_all_tiles_and_invalid_v2_mask_never_forwards(self):
        for version, mask, expected_forward in [(1, None, True), (2, 1 << 48, False)]:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                receiver = HeaderReceiver(directory)
                renderer = Renderer()
                receiver.refinement = TileRefinementController(renderer)
                receiver.player = SimpleNamespace(pid=123, poll=lambda: None)
                receiver.begin_refinement()
                receiver.refinement_on_header(10, ALL_TILES)
                epoch = receiver.refinement.active_generation()[0]
                for tile in (0, 47):
                    receiver.refinement.apply(*tile_message(epoch, tile))
                left, right = socket.socketpair()
                worker = threading.Thread(target=receiver.session, args=(left,))
                worker.start()
                try:
                    wire = HEADER.pack(b"H264", version, 1, 5, 11, 123, 0)
                    if version == 2:
                        wire += DIRTY_MASK.pack(mask)
                    right.sendall(wire + b"frame")
                    if expected_forward:
                        self.assertTrue(receiver.admitted.wait(1))
                        self.assertEqual(receiver.forwarded, [(11, b"frame", 0)])
                    else:
                        worker.join(1)
                        self.assertFalse(worker.is_alive())
                        self.assertFalse(receiver.forwarded)
                        self.assertIn("dirty tile mask", receiver.stats["error"])
                finally:
                    right.close()
                    worker.join(2)
                    left.close()
                    receiver.refinement.close()

    def test_au_v2_strips_mask_and_removes_dirty_tile_before_payload_read(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = HeaderReceiver(directory)
            renderer = Renderer()
            receiver.refinement = TileRefinementController(renderer)
            receiver.player = SimpleNamespace(pid=123, poll=lambda: None)
            receiver.begin_refinement()
            receiver.refinement_on_header(10, ALL_TILES)
            epoch = receiver.refinement.active_generation()[0]
            for tile in (0, 1):
                receiver.refinement.apply(*tile_message(epoch, tile))
            entered, release = threading.Event(), threading.Event()
            renderer.before_remove = lambda: (entered.set(), release.wait(1))
            left, right = socket.socketpair()
            worker = threading.Thread(target=receiver.session, args=(left,))
            worker.start()
            try:
                right.sendall(HEADER.pack(b"H264", 2, 1, 5, 11, 123, 0) + DIRTY_MASK.pack(1))
                self.assertTrue(entered.wait(1))
                self.assertEqual(receiver.refinement.last_dirty[:2], [11, 10])
                self.assertEqual(receiver.stats["received_frames"], 0)
                self.assertFalse(receiver.forwarded)
                release.set()
                right.sendall(b"frame")
                self.assertTrue(receiver.admitted.wait(1))
                self.assertEqual(receiver.forwarded, [(11, b"frame", 2)])
            finally:
                release.set()
                right.close()
                worker.join(2)
                left.close()
                receiver.refinement.close()


if __name__ == "__main__":
    unittest.main()
