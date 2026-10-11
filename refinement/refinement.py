#!/usr/bin/env python3
"""Isolated, opt-in RGB idle refinement. Does not launch or alter any service.

Integration must call on_video_frame() BEFORE forwarding each new H264 AU.
Only a successfully completed call is permission to forward it. No frame-size
allocation, pixel processing or network command occurs on steady motion when
there is no overlay. All RGB work belongs to the idle snapshot worker.
"""
from __future__ import annotations

import json
import os
import select
import socket
import struct
import tempfile
import threading
import time
import uuid
import zlib

PORT = 16011
MAX_HEADER = 4096
MAX_PAYLOAD = 10 * 1024 * 1024
MAX_RAW = 1920 * 1080 * 4
MAX_SEQUENCE = (1 << 63) - 1
PREFIX = struct.Struct(">I")
OVERLAY_ID = 0  # Reserved exclusively by this controller, not shared scripts.
TILE_COLUMNS, TILE_ROWS = 8, 6
TILE_WIDTH, TILE_HEIGHT = 240, 180
TILE_COUNT = TILE_COLUMNS * TILE_ROWS
ALL_TILES = (1 << TILE_COUNT) - 1
MAX_TILE_PAYLOAD = 256 * 1024


class ProtocolError(ValueError):
    pass


class CleanEOF(EOFError):
    """Peer closed exactly between messages, not inside a header or payload."""


class RendererError(RuntimeError):
    """Fail closed: do not forward video until an overlay clear succeeds."""


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError("invalid integer")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def read_exact(stream, size, deadline=None, allow_clean_eof=False):
    chunks = bytearray(size)
    view = memoryview(chunks)
    offset = 0
    while offset < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("refinement message deadline")
            stream.settimeout(remaining)
        count = stream.recv_into(view[offset:])
        if not count:
            if allow_clean_eof and offset == 0:
                raise CleanEOF("refinement message boundary")
            raise EOFError("truncated refinement message")
        offset += count
    return chunks


def read_header(stream, deadline=None):
    length, = PREFIX.unpack(read_exact(stream, PREFIX.size, deadline, allow_clean_eof=True))
    if not 0 < length <= MAX_HEADER:
        raise ProtocolError("header length exceeds limit")
    try:
        obj = json.loads(read_exact(stream, length, deadline), object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProtocolError("invalid JSON") from error
    if not isinstance(obj, dict):
        raise ProtocolError("header must be an object")
    return obj


def write_header(stream, obj):
    body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
    if not 0 < len(body) <= MAX_HEADER:
        raise ProtocolError("header length exceeds limit")
    stream.sendall(PREFIX.pack(len(body)) + body)


def validate_header(header, width, height):
    expected = {"version", "kind", "epoch", "sequence", "width", "height",
                "stride", "encoding", "payload_bytes"}
    if set(header) != expected:
        raise ProtocolError("unexpected header fields")
    if type(header["version"]) is not int or header["version"] != 1:
        raise ProtocolError("unsupported version")
    if header["kind"] != "snapshot" or header["encoding"] != "bgra-zlib":
        raise ProtocolError("unsupported message or encoding")
    epoch = header["epoch"]
    if not isinstance(epoch, str) or len(epoch) != 36:
        raise ProtocolError("invalid epoch")
    try:
        if str(uuid.UUID(epoch)) != epoch:
            raise ProtocolError("noncanonical epoch")
    except ValueError as error:
        raise ProtocolError("invalid epoch") from error
    _integer(header["sequence"], 0, MAX_SEQUENCE)
    _integer(header["payload_bytes"], 1, MAX_PAYLOAD)
    if (_integer(header["width"], 1, 1920) != width
            or _integer(header["height"], 1, 1080) != height
            or _integer(header["stride"], 4, 1920 * 4) != width * 4):
        raise ProtocolError("snapshot must exactly match native output geometry")
    if header["stride"] * height > MAX_RAW:
        raise ProtocolError("decoded image exceeds limit")


def validate_tile_header(header):
    expected = {"version", "kind", "epoch", "sequence", "tile", "width", "height",
                "stride", "encoding", "payload_bytes"}
    if set(header) != expected or type(header["version"]) is not int or header["version"] != 2:
        raise ProtocolError("invalid tile header")
    if header["kind"] != "tile":
        raise ProtocolError("expected tile message")
    _integer(header["tile"], 0, TILE_COUNT - 1)
    _integer(header["payload_bytes"], 1, MAX_TILE_PAYLOAD)
    # Reuse all strict epoch/sequence/format/stride validation without accepting
    # a caller-specified rectangle or offset into another tile.
    base = dict(header)
    del base["tile"]
    base.update(version=1, kind="snapshot")
    validate_header(base, TILE_WIDTH, TILE_HEIGHT)


def decode_bgra(payload, expected_size):
    """RFC1950 zlib, exact size, no trailing data/second streams/zip bombs."""
    if not 0 < len(payload) <= MAX_PAYLOAD or not 0 < expected_size <= MAX_RAW:
        raise ProtocolError("payload exceeds limit")
    decoder = zlib.decompressobj(zlib.MAX_WBITS)
    try:
        # One byte beyond the bound detects overflow without expanding it.
        raw = decoder.decompress(payload, expected_size + 1)
    except zlib.error as error:
        raise ProtocolError("invalid zlib stream or checksum") from error
    if (len(raw) != expected_size or not decoder.eof
            or decoder.unconsumed_tail or decoder.unused_data):
        raise ProtocolError("incorrect decoded size or trailing compressed data")
    if raw[3::4].count(255) != expected_size // 4:
        raise ProtocolError("snapshot must be opaque BGRA")
    return raw


class MpvOverlay:
    """Dedicated JSON IPC commands; only touches the reserved bitmap ID.

    Each command uses a separate socket, so a timed-out response can never be
    mistaken for another command's ACK. A failed command raises, never silently
    authorizes forwarding new motion. mpv copies overlay files before its ACK.
    """
    def __init__(self, ipc_path, width=1920, height=1080, timeout=2.0):
        self.ipc_path = str(ipc_path)
        self.width, self.height = width, height
        self.timeout = timeout

    def command(self, command):
        return self.command_batch([command])[0]

    def command_batch(self, commands):
        if not 1 <= len(commands) <= 64:
            raise RendererError("invalid command batch size")
        deadline = time.monotonic() + self.timeout
        message = b"".join(json.dumps({"command": command, "request_id": index + 1}).encode() + b"\n"
                           for index, command in enumerate(commands))
        replies = {}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(self.ipc_path)
                client.sendall(message)
                pending = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("mpv ACK deadline")
                    client.settimeout(remaining)
                    data = client.recv(4096)
                    if not data:
                        raise EOFError("mpv disconnected before ACK")
                    pending.extend(data)
                    if len(pending) > 65536:
                        raise ValueError("mpv response exceeds limit")
                    while b"\n" in pending:
                        line, _, pending = pending.partition(b"\n")
                        response = json.loads(line)
                        if not isinstance(response, dict):
                            raise ValueError("mpv response must be an object")
                        request_id = response.get("request_id")
                        if type(request_id) is int and 1 <= request_id <= len(commands):
                            if response.get("error") != "success":
                                raise ValueError("mpv rejected command")
                            replies[request_id] = response.get("data")
                            if len(replies) == len(commands):
                                return [replies[index + 1] for index in range(len(commands))]
        except (OSError, ValueError, EOFError, RecursionError) as error:
            raise RendererError(str(error)) from error

    def add(self, path):
        self._validate_geometry()
        self.command({"name": "overlay-add", "id": OVERLAY_ID, "x": 0, "y": 0,
                      "file": str(path), "offset": 0, "fmt": "bgra",
                      "w": self.width, "h": self.height, "stride": self.width * 4,
                      "dw": self.width, "dh": self.height})

    def _validate_geometry(self):
        # OSD coordinates are physical window pixels, not video coordinates.
        # Fail closed if a window resize/scaling/letterbox would misplace RGB.
        geometry = self.command(["get_property", "osd-dimensions"])
        if (not isinstance(geometry, dict)
                or geometry.get("w") != self.width or geometry.get("h") != self.height
                or any(geometry.get(key, 0) != 0 for key in ("ml", "mr", "mt", "mb"))):
            raise RendererError("mpv must show native output without margins")

    def add_tile(self, path, tile):
        _integer(tile, 0, TILE_COUNT - 1)
        self._validate_geometry()
        self.command({"name": "overlay-add", "id": tile,
                      "x": tile % TILE_COLUMNS * TILE_WIDTH,
                      "y": tile // TILE_COLUMNS * TILE_HEIGHT,
                      "file": str(path), "offset": 0, "fmt": "bgra",
                      "w": TILE_WIDTH, "h": TILE_HEIGHT, "stride": TILE_WIDTH * 4,
                      "dw": TILE_WIDTH, "dh": TILE_HEIGHT})

    def remove_tiles(self, mask):
        _integer(mask, 0, ALL_TILES)
        if mask:
            self.command_batch([["overlay-remove", tile] for tile in range(TILE_COUNT)
                                if mask & (1 << tile)])

    def remove(self):
        self.command(["overlay-remove", OVERLAY_ID])


class RefinementController:
    """Generation state + a single atomically removable full-screen overlay.

    video_frame calls are serialized by the video receiver. The state lock is
    deliberately distinct from the renderer lock: arrival invalidates pending
    work immediately even while a snapshot is decompressing/writing/adding.
    """
    def __init__(self, renderer, width=1920, height=1080, temp_parent=None):
        _integer(width, 1, 1920)
        _integer(height, 1, 1080)
        self.renderer = renderer
        self.width, self.height = width, height
        self.state_lock = threading.Lock()
        self.render_lock = threading.Lock()
        self.epoch = None
        self.sequence = None
        self.visible = False  # Also true if an add may have reached mpv.
        self.unsafe = False  # Add/removal has not received a renderer ACK.
        self.snapshot_sequence = None  # Actual ACKed v1 image, never latest video by inference.
        self.clients = set()
        self.tempdir = tempfile.TemporaryDirectory(prefix="rgb-refinement-", dir=temp_parent)
        os.chmod(self.tempdir.name, 0o700)
        self.closed = False

    def active_generation(self):
        with self.state_lock:
            return self.epoch, self.sequence

    def status_snapshot(self):
        # Match apply's render→state lock order. Counts and hexadecimal masks
        # come from the same renderer state, including unacknowledged IDs.
        with self.render_lock:
            with self.state_lock:
                # v1 uses the single overlay ID0. It must not report zero just
                # because the v2 per-tile bitsets do not exist in this mode.
                visible = getattr(self, "visible_tiles", int(self.visible))
                unsafe = getattr(self, "unsafe_tiles", int(self.unsafe))
                return {"epoch": self.epoch, "latest_sequence": self.sequence,
                        "mode": "tiles" if hasattr(self, "visible_tiles") else "snapshot",
                        "visible": bool(visible), "unsafe": bool(unsafe),
                        "snapshot_sequence": self.snapshot_sequence,
                        "visible_tiles": visible.bit_count(), "unsafe_tiles": unsafe.bit_count(),
                        "visible_mask": f"0x{visible:012x}", "unsafe_mask": f"0x{unsafe:012x}"}

    def hello(self, epoch):
        return {"version": 1, "kind": "hello", "epoch": epoch}

    def validate(self, header):
        validate_header(header, self.width, self.height)

    def retain_after_clean_eof(self, last_accepted):
        return last_accepted

    def _matches(self, epoch, sequence):
        with self.state_lock:
            return self.epoch == epoch and self.sequence == sequence and not self.closed

    @staticmethod
    def _shutdown(clients):
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def begin_video(self):
        with self.state_lock:
            if self.closed:
                raise RuntimeError("controller is closed")
            self.epoch, self.sequence = str(uuid.uuid4()), None
            clients = list(self.clients)
            self.clients.clear()
            epoch = self.epoch
        self._shutdown(clients)
        with self.render_lock:
            self._clear_locked(force=True)
        return epoch

    def end_video(self):
        with self.state_lock:
            self.epoch = self.sequence = None
            clients = list(self.clients)
            self.clients.clear()
        self._shutdown(clients)
        with self.render_lock:
            self._clear_locked()

    def on_video_frame(self, sequence, dirty_mask=None):
        """Publish invalidation first; return only after the removal ACK.

        The caller MUST NOT feed this AU to mpv if this raises RendererError.
        Do not wait for pixel payload reads/decompression before calling this.
        """
        _integer(sequence, 0, MAX_SEQUENCE)
        with self.state_lock:
            if self.epoch is None or self.closed:
                raise RuntimeError("no active video session")
            if self.sequence is not None and sequence <= self.sequence:
                raise ProtocolError("video sequence must strictly increase")
            self.sequence = sequence
        with self.render_lock:
            self._clear_locked()

    def _clear_locked(self, force=False):
        if self.visible or force:
            # Keep visible true on failure so a retry cannot skip the barrier.
            self.visible = True
            self.unsafe = True
            self.renderer.remove()
            self.visible = False
            self.unsafe = False
            self.snapshot_sequence = None

    def attach_client(self, client):
        with self.state_lock:
            if self.epoch is None or self.closed:
                raise ProtocolError("no active video session")
            if self.clients:
                raise ProtocolError("another refinement client is connected")
            self.clients.add(client)
            return self.epoch

    def detach_client(self, client, epoch, keep_overlay=False):
        with self.state_lock:
            self.clients.discard(client)
            current = self.epoch == epoch
        if current and not keep_overlay:
            with self.render_lock:
                # Recheck: a new session may begin while waiting for the lock.
                with self.state_lock:
                    current = self.epoch == epoch
                if current:
                    self._clear_locked()

    def apply(self, header, payload):
        validate_header(header, self.width, self.height)
        if len(payload) != header["payload_bytes"]:
            raise ProtocolError("payload length differs from header")
        epoch, sequence = header["epoch"], header["sequence"]
        if not self._matches(epoch, sequence):
            return False, "stale-generation"
        raw = decode_bgra(payload, self.width * self.height * 4)
        if not self._matches(epoch, sequence):
            return False, "motion-during-decompression"
        fd, path = tempfile.mkstemp(prefix="snapshot-", suffix=".bgra", dir=self.tempdir.name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
            del raw
            with self.render_lock:
                if not self._matches(epoch, sequence):
                    return False, "motion-before-display"
                self.visible = True  # An unacknowledged add is also unsafe.
                self.unsafe = True
                self.snapshot_sequence = None
                self.renderer.add(path)
                self.unsafe = False
                self.snapshot_sequence = sequence
                if not self._matches(epoch, sequence):
                    self._clear_locked()
                    return False, "motion-during-display"
                return True, "applied"
        finally:
            # mpv consumes the file before ACK. On an IPC error integration
            # fails closed; no delayed path can apply a stale pixel file.
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def close(self):
        self.end_video()
        with self.state_lock:
            self.closed = True
        self.tempdir.cleanup()


class TileRefinementController(RefinementController):
    """48 native RGB tiles. Metadata invalidation never examines pixel memory."""
    def __init__(self, renderer, temp_parent=None):
        super().__init__(renderer, 1920, 1080, temp_parent)
        self.last_dirty = [None] * TILE_COUNT
        self.visible_tiles = 0  # Also contains every unacknowledged add/remove.
        self.unsafe_tiles = 0

    def hello(self, epoch):
        return {"version": 2, "kind": "hello", "epoch": epoch,
                "columns": TILE_COLUMNS, "rows": TILE_ROWS,
                "tile_width": TILE_WIDTH, "tile_height": TILE_HEIGHT}

    def validate(self, header):
        validate_tile_header(header)

    def retain_after_clean_eof(self, last_accepted):
        # Rejected stale tiles must not erase other already-valid tiles. Every
        # visible tile remains governed by authoritative video dirty metadata.
        return True

    def _matches_tile(self, epoch, sequence, tile):
        with self.state_lock:
            return (not self.closed and self.epoch == epoch and self.sequence is not None
                    and self.last_dirty[tile] is not None
                    and self.last_dirty[tile] <= sequence <= self.sequence)

    def on_video_frame(self, sequence, dirty_mask=None):
        _integer(sequence, 0, MAX_SEQUENCE)
        mask = ALL_TILES if dirty_mask is None else _integer(dirty_mask, 0, ALL_TILES)
        with self.state_lock:
            if self.epoch is None or self.closed:
                raise RuntimeError("no active video session")
            if self.sequence is not None and sequence <= self.sequence:
                raise ProtocolError("video sequence must strictly increase")
            if self.sequence is None:
                mask = ALL_TILES  # No dirty history before this epoch exists.
            self.sequence = sequence
            for tile in range(TILE_COUNT):
                if mask & (1 << tile):
                    self.last_dirty[tile] = sequence
        # Publish ALL per-tile generation changes before any IPC wait. The AU
        # cannot advance until every affected visible ID has acknowledged removal.
        with self.render_lock:
            self._clear_locked(mask=mask)

    def _clear_locked(self, force=False, mask=ALL_TILES):
        target = (mask if force else self.visible_tiles & mask) | self.unsafe_tiles
        if target:
            self.visible_tiles |= target
            self.unsafe_tiles |= target
            self.visible = True
            self.renderer.remove_tiles(target)
            self.visible_tiles &= ~target
            self.unsafe_tiles &= ~target
            self.visible = bool(self.visible_tiles)

    def apply(self, header, payload):
        self.validate(header)
        if len(payload) != header["payload_bytes"]:
            raise ProtocolError("payload length differs from header")
        epoch, sequence, tile = header["epoch"], header["sequence"], header["tile"]
        if not self._matches_tile(epoch, sequence, tile):
            return False, "stale-tile-generation"
        raw = decode_bgra(payload, TILE_WIDTH * TILE_HEIGHT * 4)
        if not self._matches_tile(epoch, sequence, tile):
            return False, "tile-dirtied-during-decompression"
        fd, path = tempfile.mkstemp(prefix="tile-", suffix=".bgra", dir=self.tempdir.name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
            del raw
            with self.render_lock:
                if not self._matches_tile(epoch, sequence, tile):
                    return False, "tile-dirtied-before-display"
                self.visible_tiles |= 1 << tile
                self.unsafe_tiles |= 1 << tile
                self.visible = True
                self.renderer.add_tile(path, tile)
                self.unsafe_tiles &= ~(1 << tile)
                if not self._matches_tile(epoch, sequence, tile):
                    self._clear_locked(mask=1 << tile)
                    return False, "tile-dirtied-during-display"
                return True, "applied"
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


class RefinementServer:
    """Loopback-only second channel; SSH is the authentication/encryption layer.

    No CLI/autostart by design. Receiver integration owns start/stop and video
    generation callbacks. One client, one snapshot in flight, bounded memory.
    """
    def __init__(self, controller, port=PORT, io_timeout=5.0):
        self.controller = controller
        self.io_timeout = io_timeout
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", port))
        self.socket.listen(1)
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.stopping = threading.Event()
        self.thread = None
        self.last_error = None

    def start(self):
        if self.thread is not None:
            raise RuntimeError("already started")
        self.thread = threading.Thread(target=self._run, name="rgb-refinement", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopping.is_set():
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                client.settimeout(self.io_timeout)
                epoch = None
                last_result_accepted = False
                keep_overlay = False
                try:
                    epoch = self.controller.attach_client(client)
                    write_header(client, self.controller.hello(epoch))
                    while not self.stopping.is_set():
                        # Idle sessions keep the already-rendered refinement.
                        # Only a message that has started has an I/O timeout.
                        if not select.select([client], [], [], 0.2)[0]:
                            continue
                        deadline = time.monotonic() + self.io_timeout
                        header = read_header(client, deadline)
                        self.controller.validate(header)
                        if header["epoch"] != epoch:
                            raise ProtocolError("epoch differs from handshake")
                        payload = read_exact(client, header["payload_bytes"], deadline)
                        accepted, reason = self.controller.apply(header, payload)
                        result = {"version": header["version"], "kind": "result",
                                  "sequence": header["sequence"], "accepted": accepted, "reason": reason}
                        if header["version"] == 2:
                            result["tile"] = header["tile"]
                        write_header(client, result)
                        last_result_accepted = accepted
                        if accepted:
                            # This field is serialized as server_error. A past
                            # failed client must not look like a current failure
                            # after a fresh image has been applied and ACKed.
                            self.last_error = None
                except CleanEOF:
                    # The host may send exactly one stable snapshot and close.
                    # Video sequence/epoch are authoritative invalidation; a
                    # successful idle image does not need a keepalive socket.
                    keep_overlay = self.controller.retain_after_clean_eof(last_result_accepted)
                except (ProtocolError, RendererError, OSError, EOFError) as error:
                    self.last_error = type(error).__name__ + ": " + str(error)
                finally:
                    if epoch is not None:
                        try:
                            self.controller.detach_client(client, epoch, keep_overlay=keep_overlay)
                        except RendererError as error:
                            self.last_error = str(error)

    def stop(self):
        self.stopping.set()
        self.socket.close()
        # Shut down a partially received payload before waiting on the worker.
        with self.controller.state_lock:
            clients = list(self.controller.clients)
        self.controller._shutdown(clients)
        if self.thread:
            self.thread.join(timeout=self.io_timeout + 3)
            if self.thread.is_alive():
                raise RuntimeError("refinement worker did not stop")
