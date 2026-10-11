"""Optional, bounded keyframe recovery over the existing authenticated stream.

No listener or authentication is added here. The containing video connection
must be loopback-only and transported through the user's pinned SSH tunnel.
"""
from __future__ import annotations

import collections
import os
import select
import socket
import struct
import threading
import time

CONTROL = struct.Struct(">4sBBHQQQ")
NONE_SEQUENCE = (1 << 64) - 1
HELLO, ACCEPT, REQUEST_IDR, IDR_SENT, DECLINE = range(1, 6)
CONTROL_DEADLINE = .250
MIN_REQUEST_INTERVAL = .250
RETRY_INTERVAL = 1.0


def pack(opcode, epoch, request_id=0, sequence=NONE_SEQUENCE):
    return CONTROL.pack(b"HCTL", 1, opcode, 0, epoch, request_id, sequence)


def unpack(data):
    if len(data) != CONTROL.size:
        raise ValueError("Invalid HCTL record size")
    magic, version, opcode, reserved, epoch, request_id, sequence = CONTROL.unpack(data)
    if magic != b"HCTL" or version != 1 or reserved or not epoch:
        raise ValueError("Invalid HCTL header")
    if opcode not in (HELLO, ACCEPT, REQUEST_IDR, IDR_SENT, DECLINE):
        raise ValueError("Unknown HCTL opcode")
    if opcode in (HELLO, ACCEPT, DECLINE) and (request_id or sequence != NONE_SEQUENCE):
        raise ValueError("Invalid HCTL negotiation fields")
    if opcode in (REQUEST_IDR, IDR_SENT) and not request_id:
        raise ValueError("Invalid HCTL request ID")
    return opcode, epoch, request_id, sequence


def receive_header(connection, stop):
    """Idle is unlimited; a started fixed header has a 250 ms read deadline.

The common case uses one recv. Partial video headers have the same bound as
partial control headers; large video payloads retain the receiver's own policy.
"""
    result = bytearray()
    deadline = None
    while len(result) < 32 and not stop.is_set():
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Partial stream header deadline")
            readable, _, _ = select.select([connection], [], [], remaining)
            if not readable:
                raise TimeoutError("Partial stream header deadline")
        try:
            data = connection.recv(32 - len(result))
        except (socket.timeout, BlockingIOError):
            continue
        if not data:
            raise EOFError("Sender disconnected")
        if deadline is None:
            deadline = time.monotonic() + CONTROL_DEADLINE
        result.extend(data)
    if len(result) != 32:
        raise EOFError("Receiver stopping")
    return bytes(result)


def contains_idr(payload):
    """Check the declared keyframe has an Annex-B IDR NAL (not just an ACK)."""
    offset = 0
    while True:
        offset = payload.find(b"\x00\x00\x01", offset)
        if offset < 0:
            return False
        offset += 3
        if offset < len(payload) and payload[offset] & 0x1f == 5:
            return True


class RecoveryControl:
    """One connection, one bounded reverse writer, and one coalesced recovery.

Only a complete IDR written to the decoder can finish recovery. IDR_SENT is
telemetry, never proof that the decoder has received an intact reference chain.
"""
    def __init__(self, connection, enabled=False):
        # Own a duplicate so session replacement cannot reuse this descriptor
        # while the reverse writer is still finishing its bounded syscall loop.
        self.socket = socket.socket(fileno=os.dup(connection.fileno()))
        self.enabled = enabled
        self.condition = threading.Condition()
        self.closed = False
        self.epoch = None
        self.negotiated = False
        self.reply = None
        self.reply_deadline = None
        self.last_sequence = None
        self.idrs = collections.deque(maxlen=64)
        self.requests = collections.OrderedDict()
        self.next_request_id = 1
        self.last_ack_id = 0
        self.last_send = float("-inf")
        self.pending = False
        self.pending_request_sent = False
        self.recovery_generation = 0
        self.barrier = None
        self.reason = None
        self.error = None
        self.sent_requests = 0
        self.completed_recoveries = 0
        self.coalesced_requests = 0
        self.records = collections.deque(maxlen=16)
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def on_control(self, data):
        opcode, epoch, request_id, sequence = unpack(data)
        now = time.monotonic()
        with self.condition:
            while self.records and self.records[0] <= now - 1:
                self.records.popleft()
            if len(self.records) >= 16:
                raise ValueError("HCTL record rate exceeded")
            self.records.append(now)
            if opcode == HELLO:
                if self.epoch is not None or self.last_sequence is not None:
                    raise ValueError("HCTL HELLO must precede video exactly once")
                self.epoch = epoch
                self.reply = ACCEPT if self.enabled else DECLINE
                self.reply_deadline = now + 1
                self.condition.notify_all()
                return
            if (opcode != IDR_SENT or not self.negotiated or epoch != self.epoch
                    or request_id <= self.last_ack_id or request_id not in self.requests):
                raise ValueError("Unexpected, stale, or duplicate HCTL acknowledgment")
            if sequence == NONE_SEQUENCE or sequence not in self.idrs:
                raise ValueError("HCTL acknowledgment does not name a received IDR")
            requested_after = self.requests[request_id]
            if requested_after is not None and sequence <= requested_after:
                raise ValueError("HCTL acknowledgment predates its recovery request")
            self.last_ack_id = request_id
            for old in list(self.requests):
                if old <= request_id:
                    del self.requests[old]

    def on_video(self, sequence, key):
        with self.condition:
            if self.negotiated and self.last_sequence is not None and sequence <= self.last_sequence:
                raise ValueError("Negotiated video sequences must strictly increase")
            self.last_sequence = sequence
            if key:
                self.idrs.append(sequence)

    def request(self, reason):
        """Signal only: safe while the queue lock is held; never performs I/O."""
        with self.condition:
            if self.closed:
                return
            self.coalesced_requests += int(self.pending)
            if not self.pending:
                self.pending_request_sent = False
                self.recovery_generation += 1
            self.pending = True
            self.barrier = self.last_sequence
            self.reason = reason
            self.condition.notify_all()

    def on_idr_written(self, sequence):
        with self.condition:
            if self.pending and (self.barrier is None or sequence >= self.barrier):
                self.pending = False
                self.completed_recoveries += 1
                self.reason = None
                self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"enabled": self.enabled, "hello_received": self.epoch is not None,
                    "negotiated": self.negotiated, "pending": self.pending,
                    "last_sequence": self.last_sequence, "recovery_barrier": self.barrier,
                    "reason": self.reason, "sent_requests": self.sent_requests,
                    "completed_recoveries": self.completed_recoveries,
                    "coalesced_requests": self.coalesced_requests,
                    "last_ack_request_id": self.last_ack_id,
                    "outstanding_requests": len(self.requests), "error": self.error}

    def _send(self, data, deadline):
        view = memoryview(data)
        while view:
            with self.condition:
                if self.closed:
                    raise EOFError("Recovery writer stopped")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HCTL reverse write deadline")
            try:
                written = self.socket.send(view, socket.MSG_DONTWAIT)
                if not written:
                    raise EOFError("Recovery channel closed")
                view = view[written:]
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HCTL reverse write deadline")
                select.select([], [self.socket], [], min(remaining, .050))

    def _run(self):
        try:
            while True:
                with self.condition:
                    if self.closed:
                        return
                    if self.reply is not None:
                        opcode = self.reply
                        data = pack(opcode, self.epoch)
                        deadline = min(self.reply_deadline, time.monotonic() + CONTROL_DEADLINE)
                        self.reply = None
                        request_id = None
                    elif self.negotiated and self.pending:
                        now = time.monotonic()
                        # A retry is coalesced into one bounded outstanding state.
                        interval = RETRY_INTERVAL if self.pending_request_sent else MIN_REQUEST_INTERVAL
                        delay = self.last_send + interval - now
                        if delay > 0:
                            self.condition.wait(min(delay, .250))
                            continue
                        opcode = REQUEST_IDR
                        generation = self.recovery_generation
                        request_id = self.next_request_id
                        self.next_request_id += 1
                        if request_id > NONE_SEQUENCE:
                            raise ValueError("HCTL request ID exhausted")
                        sequence = self.last_sequence
                        self.requests[request_id] = sequence
                        while len(self.requests) > 16:
                            self.requests.popitem(last=False)
                        data = pack(opcode, self.epoch, request_id,
                                    NONE_SEQUENCE if sequence is None else sequence)
                        deadline = now + CONTROL_DEADLINE
                    else:
                        self.condition.wait()
                        continue
                self._send(data, deadline)
                with self.condition:
                    if opcode == ACCEPT:
                        self.negotiated = True
                    elif opcode == REQUEST_IDR:
                        self.last_send = time.monotonic()
                        self.sent_requests += 1
                        if self.recovery_generation == generation and self.pending:
                            self.pending_request_sent = True
                    self.condition.notify_all()
        except (OSError, EOFError, ValueError) as exc:
            with self.condition:
                if not self.closed:
                    self.error = str(exc)
                    self.closed = True
            # Wake the forward reader. Partial control writes cannot be resumed
            # as a different record; a new connection must negotiate a new epoch.
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.worker.join(timeout=1)
        if self.worker.is_alive():
            raise RuntimeError("Recovery writer failed to stop")
        self.socket.close()
