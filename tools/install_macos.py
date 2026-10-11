#!/usr/bin/env python3
"""Install locally built apps without starting them or replacing user settings."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "menu"))
from connection_config import load_connection, validate_destination


def require_python(executable):
    executable = str(Path(executable).resolve())
    result = subprocess.run([executable, "-c", "import sys; raise SystemExit(sys.version_info < (3, 10))"],
                            capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError("Python 3.10 or newer is required")
    return executable


def active_apps():
    result = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, text=True,
                            timeout=10, check=True)
    names = {"WirelessDisplayMenu", "WirelessDisplayHost"}
    return [line.strip() for line in result.stdout.splitlines()
            if Path(line.strip()).name in names]


def helper_wrapper(python, helper):
    return "#!/bin/sh\nexec " + shlex.quote(str(python)) + " " + shlex.quote(str(helper)) + ' "$@"\n'


def install(root=ROOT, home=None, ssh_host=None, python=None, build_dir=None):
    home = Path.home() if home is None else Path(home)
    base = home / ".config/mac-wireless-display"
    applications = home / "Applications"
    build = Path(build_dir) if build_dir else root / "build"
    python = require_python(python or sys.executable)
    if active_apps():
        raise ValueError("Quit Wireless Display and Wireless Display Host before installing")
    connection = base / "connection.json"
    if connection.exists():
        existing_alias = load_connection(connection)
        if ssh_host is not None and validate_destination(ssh_host) != existing_alias:
            raise ValueError("Existing connection.json is preserved; edit it explicitly to change sshDestination")
    elif ssh_host is None:
        raise ValueError("Pass --ssh-host with your verified SSH alias for the first installation")
    else:
        validate_destination(ssh_host)
    files = {
        applications / "Wireless Display.app": build / "Wireless Display.app",
        base / "build/Wireless Display Host.app": build / "Wireless Display Host.app",
        base / "h264-control.py": root / "menu/h264-control.py",
        base / "connection_config.py": root / "menu/connection_config.py",
    }
    for target, source in files.items():
        if not source.exists():
            raise ValueError("Missing build or source file: " + str(source))
        if target.is_symlink():
            raise ValueError("Refusing to replace a symbolic-link installation target: " + str(target))
    defaults = {}
    for name in ("h264-settings", "display-settings"):
        target = base / (name + ".json")
        if not target.exists():
            source = root / "config" / (name + ".example.json")
            if not isinstance(json.loads(source.read_text(encoding="utf-8")), dict):
                raise ValueError("Default settings must be a JSON object: " + str(source))
            defaults[target] = source
    for path in (base, applications, base / "build", base / "h264-control", connection,
                 base / "h264-settings.json", base / "display-settings.json"):
        if path.is_symlink():
            raise ValueError("Refusing a symbolic-link installation path: " + str(path))
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    applications.mkdir(parents=True, exist_ok=True)
    (base / "build").mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = base / "backups" / stamp
    backup.mkdir(mode=0o700, parents=True)
    for path in (connection, base / "h264-settings.json", base / "display-settings.json"):
        if path.exists():
            shutil.copy2(path, backup / path.name)
    # Stage complete copies before moving any existing installation aside.
    with tempfile.TemporaryDirectory(prefix="install-", dir=base) as temporary:
        staged = Path(temporary)
        replacements = []
        for index, (target, source) in enumerate(files.items()):
            candidate = staged / str(index)
            if source.is_dir():
                shutil.copytree(source, candidate, symlinks=True)
            else:
                shutil.copy2(source, candidate)
            replacements.append((target, candidate))
        wrapper = staged / "wrapper"
        wrapper.write_text(helper_wrapper(python, base / "h264-control.py"))
        wrapper.chmod(0o700)
        replacements.append((base / "h264-control", wrapper))
        for index, (target, source) in enumerate(defaults.items()):
            candidate = staged / ("settings-" + str(index))
            shutil.copy2(source, candidate)
            candidate.chmod(0o600)
            replacements.append((target, candidate))
        if not connection.exists():
            candidate = staged / "connection"
            candidate.write_text(json.dumps({"sshDestination": ssh_host}, indent=2) + "\n")
            candidate.chmod(0o600)
            replacements.append((connection, candidate))
        if active_apps():
            raise ValueError("An app started during installation; quit it and retry")
        completed = []
        try:
            for index, (target, candidate) in enumerate(replacements):
                saved = backup / (str(index) + "-" + target.name)
                existed = target.exists()
                if existed:
                    shutil.move(str(target), saved)
                completed.append((target, saved, existed))
                shutil.move(str(candidate), target)
        except Exception:
            for target, saved, existed in reversed(completed):
                if target.exists():
                    failed = backup / ("failed-" + target.name)
                    shutil.move(str(target), failed)
                if existed:
                    shutil.move(str(saved), target)
            raise
    return backup


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", help="A verified host alias from your SSH configuration")
    parser.add_argument("--build-dir", type=Path, help="Alternative local build directory")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("The macOS installer must run on macOS")
    try:
        backup = install(ssh_host=args.ssh_host, build_dir=args.build_dir)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, "Installation stopped: " + str(exc) + "\n")
    print("Installed without launching. Existing settings were preserved. Backup: " + str(backup))
    print("Open ~/Applications/Wireless Display.app and grant the host screen-recording permission through macOS.")


if __name__ == "__main__":
    main()
