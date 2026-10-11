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
        # Refuse to steal an active stream; an idle listener can be used safely.
        active = self.command("systemctl --user is-active --quiet " + RECEIVER)
        if active.returncode == 0:
            previous = self.command(STATUS_COMMAND)
            try:
                previous_status = json.loads(previous.stdout)
                if not isinstance(previous_status, dict):
                    raise ValueError("invalid receiver status")
                if previous_status.get("connected"):
                    raise SessionError("Another video session is active on the receiver; close it first")
            except (ValueError, TypeError):
                raise SessionError("The current display receiver status could not be verified")
        self.check_cancelled()
        # Mark before requesting start so cancellation/failure cannot orphan it.
        self.receiver_owned = True
        result = self.command("systemctl --user start " + RECEIVER + " && "
                              "systemctl --user is-active --quiet " + RECEIVER, timeout=10)
        if result.returncode:
            raise SessionError("The display receiver could not start")
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
        while not self.stop.wait(1.5):
            if self.tunnel.poll() is not None:
                raise SessionError("The wireless video connection closed")
            try:
                result = self.command(STATUS_COMMAND, timeout=4)
                status = json.loads(result.stdout)
                if result.returncode or not isinstance(status, dict):
                    raise ValueError("receiver status unavailable")
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
                self.command("systemctl --user stop " + RECEIVER, timeout=5)
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
