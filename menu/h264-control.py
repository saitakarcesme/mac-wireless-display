#!/usr/bin/env python3
"""Own one menu-initiated display session; never change Wi-Fi or kill by port.

JSON events are written to stdout. The parent retains stdin: EOF or STOP ends the
session. HOST <pid> registers the bundle that the menu launched for crash cleanup.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import shlex
import uuid
import socket
import subprocess
import sys
import tempfile
import threading
import time

from connection_config import load_connection, validate_destination

BASE = Path.home() / ".config/mac-wireless-display"
HOST_BINARY = BASE / "build/Wireless Display Host.app/Contents/MacOS/WirelessDisplayHost"
PORT = 16010
REFINEMENT_PORT = 16011
RECEIVER = "mac-h264-display.service"
STATUS_COMMAND = "cat ~/.local/state/mac-h264-display/status.json"


class SessionError(Exception):
    pass


# Runs through the same authenticated SSH connection. Tokens are ownership
# markers, not credentials; all authority still comes from SSH and systemd.
REMOTE_SESSION = r"""
import fcntl, json, os, subprocess, sys
from pathlib import Path

def manage(action, client, session):
    state = Path.home() / '.local/state/mac-h264-display'
    state.mkdir(parents=True, exist_ok=True)
    owner_file = state / 'menu-owner.json'
    def read(path):
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    def run(*args):
        return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=6)
    with (state / 'menu-owner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        owner = read(owner_file)
        invocation = run('show', 'mac-h264-display.service', '-p', 'InvocationID', '--value').stdout.strip()
        ours = owner.get('client') == client and owner.get('invocation') == invocation and bool(invocation)
        if action == 'release':
            if ours and owner.get('session') == session:
                if run('stop', 'mac-h264-display.service').returncode:
                    raise RuntimeError('Receiver stop failed')
                owner_file.unlink(missing_ok=True)
            return
        active = run('is-active', '--quiet', 'mac-h264-display.service').returncode == 0
        status = read(state / 'status.json')
        if active and not ours and status.get('connected') is not False:
            raise RuntimeError('Another receiver session is active or cannot be verified')
        # A stale session from this installation can be reclaimed after Wi-Fi
        # returns. A later invocation owned by somebody else must not be stopped.
        if active and run('stop', 'mac-h264-display.service').returncode:
            raise RuntimeError('Previous receiver could not stop')
        if run('start', 'mac-h264-display.service').returncode:
            raise RuntimeError('Receiver could not start')
        invocation = run('show', 'mac-h264-display.service', '-p', 'InvocationID', '--value').stdout.strip()
        if not invocation:
            raise RuntimeError('Receiver invocation could not be verified')
        temporary = state / 'menu-owner.json.new'
        with temporary.open('w') as output:
            os.chmod(temporary, 0o600)
            json.dump({'client': client, 'session': session, 'invocation': invocation}, output)
        temporary.replace(owner_file)

if __name__ == '__main__':
    manage(*sys.argv[1:])
"""


def installation_id(base):
    path = base / 'client-id'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return str(uuid.UUID(path.read_text().strip()))
    with os.fdopen(descriptor, 'w') as output:
        value = str(uuid.uuid4())
        output.write(value + '\n')
    return value


class ReceiverHeartbeat:
    """Reject replayed telemetry without requiring synchronized wall clocks.

    No frame-count timeout: a settled RGB desktop may legitimately stay idle
    indefinitely. A dead monitor thread cannot masquerade as a healthy peer.
    """
    def __init__(self):
        self.token = None
        self.changed_at = None
        self.output = None
        self.written_at_output = 0
        self.output_at = None

    def observe(self, status, now):
        token = status.get("updated_at")
        if not isinstance(token, str) or not token:
            raise ValueError("missing receiver heartbeat")
        if token != self.token:
            self.token, self.changed_at = token, now
        elif now - self.changed_at > 12:
            raise SessionError("The receiver heartbeat stopped; reconnecting")
        # Ignore an idle frame counter. Detect a stalled output only when at
        # least four newer frames were written and playback still did not move.
        mpv = status.get("mpv") or {}
        output = mpv.get("time-pos")
        written = status.get("written_frames", 0)
        if not isinstance(output, (int, float)) or not isinstance(written, int):
            return
        generation = status.get("decoder_restarts", 0)
        marker = (generation, output)
        if marker != self.output or written < self.written_at_output:
            self.output, self.written_at_output, self.output_at = marker, written, now
        elif written - self.written_at_output >= 4 and now - self.output_at > 10:
            raise SessionError("Video playback stopped advancing; reconnecting")


def port_available(port=PORT):
    # Match OpenSSH's reuse policy so a recently closed forward in TIME_WAIT
    # does not look like a live owner. On Darwin, a reused loopback bind alone
    # can coexist with an active wildcard listener; probe that exact address
    # too. Never set SO_REUSEPORT, listen, or connect to another receiver here.
    for address in ("0.0.0.0", "127.0.0.1"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((address, port))
            except OSError:
                return False
    return True


def process_identity(pid):
    """PID plus executable and start time prevents acting on a reused PID."""
    if pid <= 1:
        return None
    try:
        command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="],
                                 capture_output=True, text=True, timeout=2)
        started = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                                 capture_output=True, text=True, timeout=2)
        executable, start = command.stdout.strip(), started.stdout.strip()
        if command.returncode or started.returncode or executable != str(HOST_BINARY) or not start:
            return None
        return executable, start
    except (OSError, subprocess.SubprocessError):
        return None


class Session:
    def __init__(self, base=BASE):
        self.base = base
        self.stop = threading.Event()
        self.target = None
        self.tunnel = None
        self.receiver_owned = False
        self.runtime = None
        self.socket_path = None
        self.log = None
        self.host = None
        self.lock = None
        self.client_id = None
        self.session_id = str(uuid.uuid4())

    @staticmethod
    def emit(event, **values):
        try:
            print(json.dumps({"event": event, **values}), flush=True)
        except BrokenPipeError:
            pass

    def ssh(self, target=None, multiplex=True):
        destination = validate_destination(target or self.target)
        args = ["/usr/bin/ssh",
                "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=3", "-o", "ConnectionAttempts=1",
                "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=2",
                "-o", "Compression=no"]
        if multiplex and self.socket_path:
            args += ["-S", str(self.socket_path)]
        return args + [destination]

    def command(self, command, *, target=None, timeout=6, multiplex=True):
        return subprocess.run(self.ssh(target, multiplex) + [command],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)

    def ownership_command(self, action):
        return "python3 -c " + shlex.quote(REMOTE_SESSION) + " " + " ".join(
            shlex.quote(value) for value in (action, self.client_id, self.session_id))

    def check_cancelled(self):
        if self.stop.is_set():
            raise SessionError("Connection cancelled")

    def read_parent(self):
        try:
            for line in sys.stdin:
                fields = line.strip().split()
                if fields == ["STOP"]:
                    break
                if len(fields) == 2 and fields[0] == "HOST" and fields[1].isdigit():
                    pid = int(fields[1])
                    identity = process_identity(pid)
                    if identity:
                        self.host = (pid, identity)
        finally:
            self.stop.set()

    def start(self):
        self.target = load_connection(self.base / "connection.json")
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = (self.base / "h264-menu.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SessionError("Another display connection process is already running")
        self.client_id = installation_id(self.base)
        if not port_available() or not port_available(REFINEMENT_PORT):
            raise SessionError("Another display connection is active; close it first")
        threading.Thread(target=self.read_parent, daemon=True).start()
        self.runtime = tempfile.TemporaryDirectory(prefix="h264-menu-", dir=self.base)
        self.socket_path = Path(self.runtime.name) / "ssh.sock"
        self.log = (self.base / "h264-menu-ssh.log").open("ab", buffering=0)
        self.emit("starting", message="Connecting to the display receiver…")
        self.check_cancelled()
        try:
            reachable = self.command("true", multiplex=False)
        except subprocess.TimeoutExpired as exc:
            raise SessionError("SSH connection timed out; check your configured destination") from exc
        if reachable.returncode:
            raise SessionError("SSH connection failed; verify the destination, host key and unattended login")
        self.check_cancelled()
        # Claim and release are serialized remotely and scoped to an exact
        # service invocation. Old cleanup cannot terminate a replacement session.
        self.receiver_owned = True
        result = self.command(self.ownership_command("claim"), timeout=16)
        if result.returncode:
            raise SessionError("Receiver is busy or could not start; another session is preserved")
        self.check_cancelled()
        args = self.ssh(multiplex=False)
        destination = args.pop()
        args += ["-M", "-S", str(self.socket_path), "-o", "ControlPersist=no",
                 "-o", "ExitOnForwardFailure=yes", "-N", "-L",
                 f"127.0.0.1:{PORT}:127.0.0.1:{PORT}", "-L",
                 f"127.0.0.1:{REFINEMENT_PORT}:127.0.0.1:{REFINEMENT_PORT}", destination]
        self.tunnel = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=self.log)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.check_cancelled()
            if self.tunnel.poll() is not None:
                raise SessionError("The wireless display connection could not open")
            if self.socket_path.exists():
                # Readiness checks listener existence without opening its single
                # receiver connection or consuming the first video packet.
                result = self.command("ss -H -ltn '( sport = :16010 or sport = :16011 )'", timeout=4)
                if (result.returncode == 0 and "127.0.0.1:16010" in result.stdout
                        and "127.0.0.1:16011" in result.stdout):
                    self.emit("ready", address=self.target)
                    return
            self.stop.wait(0.15)
        raise SessionError("The display receiver was not ready in time")

    def monitor(self):
        failures = 0
        heartbeat = ReceiverHeartbeat()
        while not self.stop.wait(1.5):
            if self.tunnel.poll() is not None:
                raise SessionError("The wireless video connection closed")
            try:
                result = self.command(STATUS_COMMAND, timeout=4)
                status = json.loads(result.stdout)
                if result.returncode or not isinstance(status, dict):
                    raise ValueError("receiver status unavailable")
                heartbeat.observe(status, time.monotonic())
                failures = 0
                self.emit("status", receiver=status)
            except (subprocess.TimeoutExpired, ValueError, TypeError):
                failures += 1
                if failures >= 2:
                    raise SessionError("The display receiver is not responding")

    def stop_host(self):
        if self.host:
            pid, identity = self.host
            if process_identity(pid) == identity:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def cleanup(self):
        self.stop_host()
        if self.receiver_owned and self.target:
            try:
                self.command(self.ownership_command("release"), timeout=8)
            except (OSError, subprocess.SubprocessError):
                pass
        if self.tunnel:
            self.tunnel.terminate()
            try:
                self.tunnel.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.tunnel.kill()
                self.tunnel.wait(timeout=2)
        if self.log:
            self.log.close()
        if self.runtime:
            self.runtime.cleanup()
        if self.lock:
            self.lock.close()


def main():
    session = Session()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: session.stop.set())
    code = 0
    try:
        session.start()
        session.monitor()
    except (SessionError, ValueError) as exc:
        if not session.stop.is_set():
            session.emit("error", message=str(exc))
            code = 1
    except (OSError, subprocess.SubprocessError):
        session.emit("error", message="The display connection failed; check the receiver and SSH connection")
        code = 1
    finally:
        session.cleanup()
        session.emit("stopped")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
