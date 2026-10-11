#!/usr/bin/env python3
"""Local-only H.264 access-unit receiver. Reach it through an SSH local forward.

Wire header: >4sHHIQQI (magic H264, version 1, flags bit 0 IDR,
payload length, encoded sequence, capture monotonic ns, reserved 0).
Version 2 adds an 8-byte big-endian dirty tile mask after that header; its
payload length still counts Annex-B only. Bits 0..47 name 8x6 native RGB tiles.
Each payload is a complete Annex-B access unit; every IDR includes SPS/PPS.
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import threading
import time

HEADER = struct.Struct(">4sHHIQQI")
DIRTY_MASK = struct.Struct(">Q")
ALL_DIRTY_TILES = (1 << 48) - 1
MAX_PAYLOAD = 4 * 1024 * 1024


@dataclasses.dataclass
class Frame:
    sequence: int
    capture_ns: int
    key: bool
    data: bytes
    arrival: float


@dataclasses.dataclass
class FrameWrite:
    reason: str | None
    bytes_written: int
    bytes_total: int
    elapsed_ms: float
    age_at_start_ms: float
    age_at_end_ms: float

    @property
    def complete(self):
        return self.reason is None and self.bytes_written == self.bytes_total

    @property
    def decoder_broken(self):
        # A deadline before the first byte leaves the previous complete AU
        # intact. A partial AU cannot be repaired by simply appending an IDR.
        return (not self.complete and self.bytes_written > 0
                or self.reason in ("decoder-exited", "decoder-pipe-closed"))


class PresentationTracker:
    """Bounded, optional arrival-to-mpv-output-notification measurements.

With correct-pts=no and fps=60, successful complete AUs receive contiguous
output indices regardless of transport sequence gaps. This maps output PTS to
that write order. It is valid only while the decoder outputs every complete AU.
IPC notification follows VO submission; it is NOT a physical scanout timestamp.
"""
    def __init__(self, max_pending=512, max_samples=4096):
        self.lock = threading.Lock()
        self.max_pending = max_pending
        self.max_samples = max_samples
        self.generation = 0
        self.reset()

    def reset(self, now=None):
        with self.lock:
            self.generation += 1
            self.started = time.monotonic() if now is None else now
            self.pending = collections.OrderedDict()
            self.early_events = {}
            self.samples = collections.deque(maxlen=self.max_samples)
            self.next_index = 0
            self.last_index = -1
            self.last_sequence = None
            self.last_observation = None
            self.observed_count = 0
            self.coalesced_count = 0
            self.expired_count = 0
            self.valid = True
            self.invalid_reason = None
            self.observer_error = None
            return self.generation

    def current_generation(self):
        with self.lock:
            return self.generation

    def invalidate(self, reason, generation=None):
        with self.lock:
            if generation is None or generation == self.generation:
                self.valid = False
                self.invalid_reason = reason
                self.early_events.clear()

    def record_write(self, frame):
        with self.lock:
            index = self.next_index
            self.next_index += 1
            self.pending[index] = (frame.sequence, frame.arrival)
            while len(self.pending) > self.max_pending:
                self.pending.popitem(last=False)
                self.expired_count += 1
            if index in self.early_events:
                self._observe_locked(index, self.early_events.pop(index))

    def observe_pts(self, pts, now=None, generation=None):
        if not isinstance(pts, (int, float)) or not math.isfinite(pts) or pts < 0:
            return
        index = round(pts * 60)
        observed = time.monotonic() if now is None else now
        with self.lock:
            if not self.valid or generation is not None and generation != self.generation:
                return
            if abs(pts * 60 - index) > .01:
                self.valid = False
                self.invalid_reason = "output PTS is not on the configured 60 Hz frame grid"
                return
            # time-pos=0 may be available during loading before any output.
            # Excluding the first frame avoids confusing initial seek PTS with VO.
            if index == 0 or index <= self.last_index:
                return
            if index >= self.next_index:
                # Output can be observed between os.write returning and its
                # accounting update on another thread. Bound this race buffer.
                if index > self.next_index + 16 or len(self.early_events) >= 16:
                    self.valid = False
                    self.invalid_reason = "output index exceeds complete AU write accounting"
                    return
                self.early_events[index] = observed
                return
            self._observe_locked(index, observed)

    def _observe_locked(self, index, observed):
        if not self.valid or index <= self.last_index:
            return
        record = self.pending.get(index)
        self.coalesced_count += max(0, index - max(self.last_index, 0) - 1)
        self.last_index = index
        self.last_observation = observed
        while self.pending and next(iter(self.pending)) <= index:
            self.pending.popitem(last=False)
        if record is None:
            return
        sequence, arrival = record
        latency = (observed - arrival) * 1000
        if latency < 0:
            self.valid = False
            self.invalid_reason = "output observation predates frame arrival"
            return
        self.last_sequence = sequence
        self.observed_count += 1
        self.samples.append((latency, arrival - self.started >= 1.0))

    @staticmethod
    def percentiles(values):
        if not values:
            return None
        values = sorted(values)
        def pick(q):
            return round(values[min(len(values) - 1, math.ceil(q * len(values)) - 1)], 3)
        return {"count": len(values), "p50": pick(.50), "p95": pick(.95),
                "p99": pick(.99), "maximum": round(values[-1], 3)}

    def snapshot(self):
        with self.lock:
            return {
                "stage": "receiver arrival to mpv VO-submission IPC notification; not scanout",
                "mapping_valid": self.valid, "invalid_reason": self.invalid_reason,
                "generation": self.generation, "complete_au_writes": self.next_index,
                "observed_frames": self.observed_count,
                "coalesced_or_unobserved_frames": self.coalesced_count,
                "expired_tracking_entries": self.expired_count,
                "pending_tracking_entries": len(self.pending),
                "early_ipc_events": len(self.early_events),
                "last_output_index": self.last_index, "last_output_sequence": self.last_sequence,
                "last_observation_monotonic": self.last_observation,
                "observer_error": self.observer_error,
                "arrival_to_vo_notification_ms": self.percentiles([x[0] for x in self.samples]),
                "steady_arrival_to_vo_notification_ms": self.percentiles(
                    [x[0] for x in self.samples if x[1]]),
            }


def mpv_arguments(ipc: Path, headless=False, renderer="gpu"):
    args = [
        "mpv", "--no-config", "--profile=low-latency", "--audio=no",
        "--untimed", "--cache=no", "--cache-pause=no",
        "--demuxer-readahead-secs=0", "--demuxer-max-bytes=64KiB",
        # Skip other demuxers' initial probes, which can wait for many tiny AUs.
        "--demuxer=+lavf", "--demuxer-max-back-bytes=0", "--demuxer-lavf-format=h264",
        "--demuxer-lavf-buffersize=4096", "--stream-buffer-size=4KiB",
        "--demuxer-lavf-probe-info=no", "--demuxer-lavf-probesize=32",
        "--demuxer-lavf-o=fflags=+nobuffer+discardcorrupt",
        # mpv overrides vd-lavc-threads with hwdec-threads for VAAPI; its default
        # of 4 retains three decoded frames even though this stream has no B frames.
        "--vd-lavc-threads=1", "--hwdec-threads=1", "--vd-lavc-o=flags=+low_delay",
        "--hwdec-extra-frames=2",
        "--vaapi-device=/dev/dri/renderD128", "--correct-pts=no", "--container-fps-override=60",
        "--hwdec-software-fallback=no", "--interpolation=no",
        "--scale=bilinear", "--cscale=bilinear", "--dscale=bilinear",
        "--dither-depth=no", "--dither=no", "--correct-downscaling=no",
        "--linear-downscaling=no", "--sigmoid-upscaling=no", "--video-unscaled=yes",
        "--video-sync=desync", "--input-default-bindings=no",
        "--input-cursor=no", "--input-terminal=no", "--osc=no",
        "--osd-level=0", "--cursor-autohide=always", "--no-border",
        "--title=Mac Wi-Fi Display", "--wayland-app-id=org.local.macdisplayh264",
        "--input-ipc-server=" + str(ipc), "--msg-level=all=warn,vd=info,vo=info",
    ]
    if headless:
        # Copy mode permits headless verification. Live mode uses direct VAAPI.
        args += ["--vo=null", "--hwdec=vaapi-copy"]
    elif renderer == "dmabuf-wayland":
        args += ["--vo=dmabuf-wayland", "--hwdec=vaapi", "--fullscreen"]
    else:
        # Bound GPU work to one frame; retain Wayland's compositor frame pacing.
        args += ["--vo=gpu", "--gpu-context=wayland", "--swapchain-depth=1",
                 "--hwdec=vaapi", "--fullscreen"]
    return args + ["-"]


class FrameQueue:
    """Bound application backlog; never feed a P frame with missing references."""
    def __init__(self, max_age: float):
        self.frames = collections.deque()
        self.condition = threading.Condition()
        self.max_age = max_age
        self.need_key = True
        self.closed = False
        self.dropped = 0
        self.reset = False
        self.grace_until = 0.0
        self.max_bytes = 8 * 1024 * 1024
        self.max_frames = 120
        self.reset_reasons = collections.Counter()
        self.last_reset_reason = None

    def _invalidate_locked(self, reason):
        self.dropped += len(self.frames)
        self.frames.clear()
        self.need_key = True
        self.reset = True
        self.last_reset_reason = reason
        self.reset_reasons[reason] += 1

    def warmup(self):
        with self.condition:
            self.grace_until = time.monotonic() + 1.0

    def invalidate(self, reason="sequence-gap", extra_dropped=0):
        with self.condition:
            self.dropped += extra_dropped
            self._invalidate_locked(reason)
            self.condition.notify_all()

    def put(self, frame: Frame):
        with self.condition:
            if (self.frames and time.monotonic() > self.grace_until
                    and frame.arrival - self.frames[0].arrival > self.max_age):
                self._invalidate_locked("queue-age")
            if (len(self.frames) >= self.max_frames or
                    sum(len(f.data) for f in self.frames) + len(frame.data) > self.max_bytes):
                self._invalidate_locked("queue-capacity")
            if self.need_key and not frame.key:
                self.dropped += 1
                return
            if frame.key:
                self.need_key = False
            self.frames.append(frame)
            self.condition.notify()

    def get(self):
        with self.condition:
            while not self.frames and not self.closed:
                self.condition.wait(0.25)
            reset = self.reset
            self.reset = False
            if self.frames:
                frame = self.frames.popleft()
                if (time.monotonic() > self.grace_until
                        and time.monotonic() - frame.arrival > self.max_age):
                    self.dropped += 1
                    self._invalidate_locked("consumer-age")
                    return None, True
                return frame, reset
            return None, reset

    def close(self, discard=False):
        with self.condition:
            self.closed = True
            if discard:
                self.dropped += len(self.frames)
                self.frames.clear()
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"dropped": self.dropped, "queue_frames": len(self.frames),
                    "queue_bytes": sum(len(f.data) for f in self.frames),
                    "waiting_for_idr": self.need_key,
                    "reset_reasons": dict(self.reset_reasons),
                    "last_reset_reason": self.last_reset_reason}


class Receiver:
    def __init__(self, args):
        self.args = args
        self.state = Path(args.state_dir).expanduser()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.ipc = self.state / "mpv.sock"
        self.stop = threading.Event()
        self.player = None
        self.player_log = None
        self.stats = {"connected": False, "received_frames": 0, "written_frames": 0,
                      "dropped_frames": 0, "decoder_restarts": 0}
        self.stats_lock = threading.Lock()
        self.active_connection = None
        self.session_thread = None
        self.consumer_thread = None
        self.presentation = (PresentationTracker()
                             if getattr(args, "measure_presentation", False) else None)
        self.refinement = None
        self.refinement_server = None
        self.refinement_lock = threading.RLock()
        self.refinement_active = False
        self.latest_video_sequence = None
        if getattr(args, "refinement_port", 0):
            # Installed beside receiver.py; source checkout keeps the optional
            # module in a sibling directory. Nothing is imported when disabled.
            import sys
            sibling = Path(__file__).resolve().parent.parent / "refinement"
            if (sibling / "refinement.py").is_file():
                sys.path.insert(0, str(sibling))
            from refinement import MpvOverlay, RefinementController, TileRefinementController, RefinementServer
            controller = TileRefinementController if getattr(args, "refinement_tiles", False) else RefinementController
            self.refinement = controller(MpvOverlay(self.ipc))
            self.refinement_server = RefinementServer(self.refinement, port=args.refinement_port)

    def end_refinement(self):
        if self.refinement is not None:
            with self.refinement_lock:
                self.refinement_active = False
                try:
                    self.refinement.end_video()
                except RuntimeError as exc:
                    # The owning caller terminates the decoder immediately;
                    # an uncertain overlay can never survive that process exit.
                    with self.stats_lock:
                        self.stats["refinement_error"] = str(exc)

    def begin_refinement(self):
        if self.refinement is None:
            return
        deadline = time.monotonic() + 2
        while not self.stop.is_set():
            if self.player.poll() is not None:
                raise RuntimeError("mpv exited before refinement IPC became ready")
            try:
                pid = self.refinement.renderer.command(["get_property", "pid"])
                if pid == self.player.pid:
                    break
            except RuntimeError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("refinement IPC startup deadline")
            self.stop.wait(.01)
        else:
            raise RuntimeError("receiver stopped during refinement initialization")
        with self.refinement_lock:
            self.refinement.begin_video()
            # Headers arriving during mpv initialization could not apply an RGB
            # snapshot: no active epoch existed. Publish their newest sequence
            # exactly once before allowing the first AU into the new player.
            if self.latest_video_sequence is not None:
                self.refinement.on_video_frame(self.latest_video_sequence)
            self.refinement_active = True

    def refinement_on_header(self, sequence, dirty_mask=None):
        if self.refinement is not None:
            with self.refinement_lock:
                self.latest_video_sequence = sequence
                if self.refinement_active:
                    # Publish invalidation before waiting for payload bytes or
                    # queue admission. A removal failure aborts this video stream.
                    if dirty_mask is None:
                        self.refinement.on_video_frame(sequence)
                    else:
                        self.refinement.on_video_frame(sequence, dirty_mask)

    def stop_player(self):
        self.end_refinement()
        if self.player:
            self.player.terminate()
            try:
                self.player.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.player.kill()
                self.player.wait()
            self.player.stdin.close()
            self.player = None
        if self.player_log:
            self.player_log.close()
            self.player_log = None

    def start_player(self):
        self.stop_player()
        self.player_log = (self.state / "mpv.log").open("ab", buffering=0)
        self.player_log.write(("\nNEW STREAM " + time.strftime("%FT%T%z") + "\n").encode())
        env = os.environ.copy()
        self.player = subprocess.Popen(mpv_arguments(self.ipc, self.args.headless, self.args.renderer),
                                       stdin=subprocess.PIPE, stdout=self.player_log,
                                       stderr=self.player_log, env=env, bufsize=0)
        os.set_blocking(self.player.stdin.fileno(), False)
        with self.stats_lock:
            self.stats["decoder_restarts"] += 1
        if self.presentation:
            generation = self.presentation.reset()
            threading.Thread(target=self.observe_presentation,
                             args=(generation, self.player), daemon=True).start()
        self.begin_refinement()

    def observe_presentation(self, generation, player):
        tracker = self.presentation
        deadline = time.monotonic() + 2
        ipc = None
        try:
            while not self.stop.is_set() and tracker.current_generation() == generation:
                if player.poll() is not None:
                    return
                try:
                    ipc = socket.socket(socket.AF_UNIX)
                    ipc.settimeout(.25)
                    ipc.connect(str(self.ipc))
                    break
                except OSError:
                    ipc.close()
                    ipc = None
                    if time.monotonic() > deadline:
                        raise TimeoutError("mpv IPC did not become available within two seconds")
                    self.stop.wait(.01)
            if ipc is None:
                return
            for number, prop in enumerate(("time-pos", "frame-drop-count", "decoder-frame-drop-count"), 1):
                ipc.sendall((json.dumps({"command": ["observe_property", number, prop]}) + "\n").encode())
            pending = b""
            while not self.stop.is_set() and tracker.current_generation() == generation:
                try:
                    data = ipc.recv(65536)
                except socket.timeout:
                    if player.poll() is not None:
                        return
                    continue
                if not data:
                    return
                observed = time.monotonic()
                pending += data
                if len(pending) > 1024 * 1024:
                    raise ValueError("mpv IPC response exceeded bounded parser buffer")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("event") != "property-change":
                        continue
                    name, value = message.get("name"), message.get("data")
                    if name == "time-pos":
                        tracker.observe_pts(value, observed, generation)
                    elif name in ("frame-drop-count", "decoder-frame-drop-count") and value:
                        tracker.invalidate("mpv reported " + name, generation)
        except (OSError, ValueError) as exc:
            with tracker.lock:
                if tracker.generation == generation:
                    tracker.observer_error = str(exc)
        finally:
            if ipc is not None:
                ipc.close()

    def write_frame(self, frame, grace_until):
        started = time.monotonic()
        deadline = max(frame.arrival + self.args.max_queue_ms / 1000, grace_until)
        data = memoryview(frame.data)
        total = len(data)

        def result(reason=None):
            now = time.monotonic()
            return FrameWrite(reason, total - len(data), total,
                              round((now - started) * 1000, 3),
                              round((started - frame.arrival) * 1000, 3),
                              round((now - frame.arrival) * 1000, 3))

        fd = self.player.stdin.fileno()
        while data and not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result("pipe-write-deadline")
            if self.player.poll() is not None:
                return result("decoder-exited")
            try:
                # POLLOUT may require PIPE_BUF bytes free even when this small
                # access unit fits. Try the nonblocking write before polling.
                written = os.write(fd, data)
                if written == 0:
                    return result("decoder-pipe-closed")
                data = data[written:]
            except BlockingIOError:
                _, writable, _ = select.select([], [fd], [], remaining)
                if not writable:
                    return result("pipe-write-deadline")
            except BrokenPipeError:
                return result("decoder-pipe-closed")
        return result(None if not data else "receiver-stopping")

    def consume(self, queue, dropped_base):
        while not self.stop.is_set():
            frame, reset = queue.get()
            if reset:
                # A dropped COMPLETE access unit needs a fresh reference chain,
                # not a new player window. The next SPS/PPS+IDR safely replaces
                # the decoder's reference picture buffer, without visible flashes.
                with self.stats_lock:
                    self.stats["last_recovery_reason"] = queue.last_reset_reason
            if frame is None:
                if queue.closed:
                    return
                continue
            if self.player is None:
                if not frame.key:
                    queue.invalidate("missing-initial-idr")
                    continue
                try:
                    self.start_player()
                except (OSError, RuntimeError) as exc:
                    with self.stats_lock:
                        self.stats["error"] = str(exc)
                    queue.close(discard=True)
                    if self.active_connection:
                        try:
                            self.active_connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    self.stop_player()
                    return
                # EGL startup gets a one-second grace for the complete queue.
                # Steady-state queue age remains bounded at max_queue_ms.
                queue.warmup()
            result = self.write_frame(frame, queue.grace_until)
            diagnostic = dataclasses.asdict(result)
            diagnostic.update(sequence=frame.sequence, keyframe=frame.key)
            with self.stats_lock:
                self.stats["last_frame_write"] = diagnostic
                self.stats["maximum_pipe_write_ms"] = max(
                    result.elapsed_ms, self.stats.get("maximum_pipe_write_ms", 0))
            if not result.complete:
                queue.invalidate(result.reason, extra_dropped=1)
                with self.stats_lock:
                    self.stats["last_failed_frame_write"] = diagnostic
                    self.stats["dropped_frames"] = dropped_base + queue.dropped
                    failures = self.stats.setdefault("write_failure_reasons", {})
                    failures[result.reason] = failures.get(result.reason, 0) + 1
                    if result.decoder_broken:
                        self.stats["last_decoder_restart_reason"] = result.reason
                if result.decoder_broken:
                    self.stop_player()
                continue
            if getattr(self, "presentation", None):
                self.presentation.record_write(frame)
            with self.stats_lock:
                self.stats["written_frames"] += 1
                self.stats["last_written_sequence"] = frame.sequence
                self.stats["dropped_frames"] = dropped_base + queue.dropped

    def receive_exact(self, connection, size):
        result = bytearray()
        while len(result) < size and not self.stop.is_set():
            try:
                data = connection.recv(size - len(result))
            except socket.timeout:
                continue
            if not data:
                raise EOFError()
            result.extend(data)
        if len(result) != size:
            raise EOFError()
        return bytes(result)

    def session(self, connection):
        with self.refinement_lock:
            self.latest_video_sequence = None
        queue = FrameQueue(self.args.max_queue_ms / 1000)
        queue.warmup()
        with self.stats_lock:
            dropped_base = self.stats["dropped_frames"]
        worker = threading.Thread(target=self.consume, args=(queue, dropped_base), daemon=True)
        self.consumer_thread = worker
        worker.start()
        sequence = None
        minimum_offset = None
        with self.stats_lock:
            self.stats.update(connected=True, error=None)
        try:
            while not self.stop.is_set():
                magic, version, flags, size, seq, captured, reserved = HEADER.unpack(
                    self.receive_exact(connection, HEADER.size))
                if (magic != b"H264" or version not in (1, 2) or flags & ~1 or reserved
                        or not 0 < size <= MAX_PAYLOAD):
                    raise ValueError("Invalid H264 header")
                dirty_mask = None
                if version == 2:
                    dirty_mask, = DIRTY_MASK.unpack(self.receive_exact(connection, DIRTY_MASK.size))
                    if dirty_mask & ~ALL_DIRTY_TILES:
                        raise ValueError("Invalid H264 dirty tile mask")
                self.refinement_on_header(seq, dirty_mask)
                payload = self.receive_exact(connection, size)
                arrival = time.monotonic()
                offset = arrival - captured / 1e9
                minimum_offset = min(minimum_offset, offset) if minimum_offset is not None else offset
                if sequence is not None and seq != sequence + 1:
                    queue.invalidate()
                sequence = seq
                queue.put(Frame(seq, captured, bool(flags & 1), payload, arrival))
                with self.stats_lock:
                    self.stats["received_frames"] += 1
                    self.stats.update(last_sequence=seq, last_receive_monotonic=arrival,
                                      payload_bytes=size, dropped_frames=dropped_base + queue.dropped,
                                      queue=queue.snapshot(),
                                      relative_excess_delay_ms=round((offset - minimum_offset) * 1000, 2))
        except (EOFError, OSError, ValueError, RuntimeError) as exc:
            with self.stats_lock:
                self.stats.update(connected=False, error=str(exc) or "Sender disconnected")
        finally:
            queue.close(discard=True)
            worker.join(timeout=2)
            if worker.is_alive():
                # The consumer still owns the shared player. Leave its handle
                # intact, and prohibit replacement until that owner has exited.
                with self.stats_lock:
                    self.stats["error"] = "Decoder consumer failed to stop within 2 seconds"
                raise RuntimeError(self.stats["error"])
            self.consumer_thread = None
            self.stop_player()
            with self.stats_lock:
                self.stats["dropped_frames"] = dropped_base + queue.dropped

    def retire_session(self):
        """Interrupt a stale SSH connection before accepting a new stream."""
        if self.active_connection:
            try:
                self.active_connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.active_connection.close()
            self.active_connection = None
        if self.session_thread:
            self.session_thread.join(timeout=3)
            if self.session_thread.is_alive():
                raise RuntimeError("Previous receiver session failed to stop within 3 seconds")
            self.session_thread = None
        consumer = getattr(self, "consumer_thread", None)
        if consumer is not None:
            if consumer.is_alive():
                raise RuntimeError("Previous decoder consumer still owns the player; replacement refused")
            self.consumer_thread = None
            self.stop_player()

    def replace_session(self, connection):
        # Only this accept thread owns session replacement. The old decoder is
        # fully stopped before a new session can use the shared player handle.
        self.retire_session()
        self.active_connection = connection
        with self.stats_lock:
            self.stats["connections_accepted"] = self.stats.get("connections_accepted", 0) + 1
        self.session_thread = threading.Thread(target=self.session, args=(connection,), daemon=True)
        self.session_thread.start()

    def mpv_stats(self):
        properties = ["hwdec-current", "video-params", "estimated-vf-fps", "display-fps",
                      "frame-drop-count", "decoder-frame-drop-count", "vo-delayed-frame-count",
                      "demuxer-cache-duration", "vo-configured", "time-pos"]
        result = {}
        try:
            with socket.socket(socket.AF_UNIX) as ipc:
                ipc.settimeout(0.15)
                ipc.connect(str(self.ipc))
                for request_id, prop in enumerate(properties, 1):
                    ipc.sendall((json.dumps({"command": ["get_property", prop],
                                             "request_id": request_id}) + "\n").encode())
                pending = b""
                deadline = time.monotonic() + 0.3
                while time.monotonic() < deadline and len(result) < len(properties):
                    data = ipc.recv(65536)
                    if not data:
                        break
                    pending += data
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        message = json.loads(line)
                        request_id = message.get("request_id", 0)
                        if 1 <= request_id <= len(properties):
                            result[properties[request_id - 1]] = message.get("data")
        except (OSError, ValueError):
            pass
        return result

    def monitor(self):
        previous_count, previous_time = 0, time.monotonic()
        previous_playback_frame, previous_decoder = None, None
        previous_playback_sample = None
        while not self.stop.wait(1):
            with self.stats_lock:
                stats = self.stats.copy()
            now = time.monotonic()
            count = stats["written_frames"]
            sample_interval = now - previous_time
            stats["written_fps"] = round((count - previous_count) / sample_interval, 2)
            previous_count, previous_time = count, now
            stats["updated_at"] = time.strftime("%FT%T%z")
            stats["mpv"] = self.mpv_stats()
            if self.presentation:
                stats["presentation"] = self.presentation.snapshot()
            if self.refinement:
                with self.refinement_lock:
                    refinement_active = self.refinement_active
                # Do not hold the video-header serialization lock while a
                # diagnostic snapshot waits for an in-flight renderer command.
                stats["refinement"] = {
                    "enabled": True, "active": refinement_active,
                    "server_error": self.refinement_server.last_error,
                    **self.refinement.status_snapshot(),
                }
            playback_sample = time.monotonic()
            playback_pts = stats["mpv"].get("time-pos")
            # --correct-pts=no --container-fps-override=60 creates one timestamp
            # step of 1/60 per decoded frame, including variable-rate arrivals.
            # Unlike estimated-frame-number, this works with an unknown duration.
            # It measures output progression, not physical compositor scanout.
            playback_frame = round(playback_pts * 60) if playback_pts is not None else None
            stats["playback_frame_progress_fps"] = None
            if (playback_frame is not None and previous_playback_frame is not None
                    and previous_playback_sample is not None
                    and previous_decoder == stats["decoder_restarts"]
                    and playback_frame >= previous_playback_frame):
                stats["playback_frame_progress_fps"] = round(
                    (playback_frame - previous_playback_frame) /
                    (playback_sample - previous_playback_sample), 2)
            previous_playback_frame = playback_frame
            previous_playback_sample = playback_sample
            previous_decoder = stats["decoder_restarts"]
            temporary = self.state / "status.json.new"
            temporary.write_text(json.dumps(stats, indent=2) + "\n")
            temporary.replace(self.state / "status.json")

    def run(self):
        threading.Thread(target=self.monitor, daemon=True).start()
        if self.refinement_server:
            self.refinement_server.start()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.args.port))
            listener.listen(1)
            listener.settimeout(1)
            while not self.stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
                connection.settimeout(1)
                self.replace_session(connection)
        self.retire_session()
        self.stop_player()
        if self.refinement_server:
            self.refinement_server.stop()
            self.refinement.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=16010)
    parser.add_argument("--state-dir", default="~/.local/state/mac-h264-display")
    parser.add_argument("--max-queue-ms", type=float, default=100)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--measure-presentation", action="store_true",
                        help="Measure bounded arrival-to-mpv-output notifications; not display scanout")
    parser.add_argument("--refinement-port", type=int, default=0,
                        help="Enable idle RGB refinement on this loopback port (for example 16011)")
    parser.add_argument("--refinement-tiles", action="store_true",
                        help="Use v2 per-tile RGB refinement; requires --refinement-port")
    parser.add_argument("--renderer", choices=["gpu", "dmabuf-wayland"], default="gpu")
    parser.add_argument("--print-mpv-command", action="store_true")
    args = parser.parse_args()
    if args.refinement_tiles and not args.refinement_port:
        parser.error("--refinement-tiles requires --refinement-port")
    if args.print_mpv_command:
        import shlex
        print(shlex.join(mpv_arguments(Path(args.state_dir).expanduser() / "mpv.sock", args.headless, args.renderer)))
        return
    receiver = Receiver(args)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: receiver.stop.set())
    receiver.run()


if __name__ == "__main__":
    main()
