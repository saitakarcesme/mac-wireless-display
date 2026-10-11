#!/usr/bin/env python3
"""Real Swift transport / Python receiver interoperability, synthetic NALs only."""
import argparse
import datetime
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'receiver'))
from receiver import Receiver, FrameWrite, HEADER
from recovery import ACCEPT, CONTROL, DECLINE, HELLO, REQUEST_IDR, NONE_SEQUENCE, pack, unpack


class SyntheticReceiver(Receiver):
    def __init__(self, state, enabled):
        super().__init__(SimpleNamespace(state_dir=state, max_queue_ms=100,
                                         recovery_control=enabled))
        self.written = []
        self.keyframes = []
        self.failed = False
        self.reference = False
        self.invalid_reference = False

    def start_player(self):
        self.player = SimpleNamespace(poll=lambda: None)
        self.reference = False
        self.stats['decoder_restarts'] += 1

    def stop_player(self):
        self.player = None
        self.reference = False

    def write_frame(self, frame, grace):
        if self.args.recovery_control and not self.failed and frame.sequence >= 4:
            self.failed = True
            self.reference = False
            return FrameWrite('synthetic-decoder-fault', 1, len(frame.data), 0, 0, 0)
        if frame.key:
            self.reference = True
            self.keyframes.append(frame.sequence)
        elif not self.reference:
            self.invalid_reference = True
        self.written.append(frame.sequence)
        return FrameWrite(None, len(frame.data), len(frame.data), 0, 0, 0)


def exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise EOFError()
        data.extend(part)
    return bytes(data)


def terminate_and_reap(process, timeout=3):
    """Bound failure cleanup, including a child that ignores SIGTERM."""
    try:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=timeout)
    finally:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def probe(binary, mode):
    with socket.socket() as listener, tempfile.TemporaryDirectory() as state:
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        listener.settimeout(4)
        command = [binary, str(listener.getsockname()[1])]
        if mode == 'host_disabled':
            command.append('off')
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        result = {'case': mode}
        connection = None
        try:
            connection, _ = listener.accept()
            connection.settimeout(4)
            if mode in ('negotiated_fault', 'receiver_declines', 'host_disabled'):
                receiver = SyntheticReceiver(state, mode != 'receiver_declines')
                if mode == 'host_disabled':
                    receiver.failed = True  # This case verifies disabled compatibility only.
                receiver.active_connection = connection
                receiver.session(connection)
                result.update(receiver=receiver.recovery.snapshot(),
                              received=receiver.stats['received_frames'],
                              written=len(receiver.written), keyframes=receiver.keyframes,
                              decoder_starts=receiver.stats['decoder_restarts'],
                              invalid_reference=receiver.invalid_reference)
                assert result['received'] > 60
                assert not receiver.invalid_reference
                if mode == 'negotiated_fault':
                    assert result['receiver']['negotiated']
                    assert result['receiver']['sent_requests'] >= 1
                    assert result['receiver']['last_ack_request_id'] >= 1
                    assert result['receiver']['completed_recoveries'] >= 1
                    assert result['decoder_starts'] == 2
                    assert result['keyframes'][1] < 30
                elif mode == 'receiver_declines':
                    assert not result['receiver']['negotiated']
                    assert result['receiver']['hello_received']
                    assert result['receiver']['sent_requests'] == 0
                    assert receiver.keyframes[0] == 0 and len(receiver.keyframes) >= 3
                    assert all(b - a <= 30 for a, b in zip(receiver.keyframes, receiver.keyframes[1:]))
                else:
                    assert not result['receiver']['hello_received']
                    assert receiver.keyframes[:3] == [0, 30, 60]
                connection.close()
            elif mode == 'legacy_fallback':
                assert unpack(exact(connection, 32))[0] == HELLO
                connection.close()
                connection, _ = listener.accept()
                connection.settimeout(4)
                keyframes = []
                count = 0
                try:
                    while True:
                        magic, version, flags, size, seq, _, _ = HEADER.unpack(exact(connection, 32))
                        assert magic == b'H264' and version == 1
                        exact(connection, size)
                        count += 1
                        if flags & 1:
                            keyframes.append(seq)
                except EOFError:
                    pass
                connection.close()
                assert count > 60 and len(keyframes) >= 3
                assert all(b - a <= 30 for a, b in zip(keyframes, keyframes[1:]))
                result.update(received=count, keyframes=keyframes)
            else:
                _, epoch, _, _ = unpack(exact(connection, 32))
                if mode == 'wrong_epoch':
                    connection.sendall(pack(ACCEPT, epoch ^ 1 or 2))
                elif mode == 'partial_record':
                    connection.sendall(pack(ACCEPT, epoch)[:7])
                else:
                    connection.sendall(pack(ACCEPT, epoch))
                    if mode == 'future_sequence':
                        connection.sendall(pack(REQUEST_IDR, epoch, 1, NONE_SEQUENCE - 1))
                    elif mode == 'replay':
                        record = pack(REQUEST_IDR, epoch, 1)
                        connection.sendall(record + record)
                    elif mode == 'malformed_reserved':
                        malformed = bytearray(pack(REQUEST_IDR, epoch, 1))
                        malformed[7] = 1
                        connection.sendall(malformed)
                    elif mode == 'flood':
                        connection.sendall(b''.join(pack(REQUEST_IDR, epoch, i) for i in range(1, 25)))
                started = time.monotonic()
                while connection.recv(4096):
                    pass
                result['disconnect_ms'] = round((time.monotonic() - started) * 1000, 3)
                assert result['disconnect_ms'] < 1500
                connection.close()
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 0, stderr
            result['host_events'] = stdout.strip().splitlines()
            if mode == 'wrong_epoch':
                assert 'wrong control epoch' in stdout
            elif mode == 'future_sequence':
                assert 'future recovery sequence' in stdout
            elif mode == 'replay':
                assert 'replayed request' in stdout
            elif mode == 'partial_record':
                assert 'partial control' in stdout
            elif mode == 'flood':
                assert 'control rate exceeded' in stdout
            elif mode == 'malformed_reserved':
                assert 'recovery control reset' in stdout
            result['passed'] = True
            return result
        finally:
            primary_error = sys.exc_info()[1]
            cleanup_errors = []
            try:
                if connection is not None:
                    connection.close()
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
            try:
                terminate_and_reap(process)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                if primary_error is None:
                    raise cleanup_errors[0]
                # Keep the assertion/protocol failure as the reported cause.
                for cleanup_error in cleanup_errors:
                    print('Probe cleanup also failed: ' + repr(cleanup_error), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    results = []
    cases = ('negotiated_fault', 'receiver_declines', 'host_disabled', 'legacy_fallback',
             'wrong_epoch', 'future_sequence', 'replay', 'partial_record', 'malformed_reserved', 'flood')
    for mode in cases:
        result = probe(args.binary, mode)
        results.append(result)
        print(mode + ': PASS', flush=True)
    document = {'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'scope': 'loopback synthetic AU bytes; no capture, decoder, GUI, or live service',
                'cases': results}
    Path(args.output).write_text(json.dumps(document, indent=2) + '\n')


if __name__ == '__main__':
    main()
