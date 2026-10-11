#!/usr/bin/env python3
"""Register an inactive user service using the current Python and Wayland session."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required")
    if sys.platform != "linux":
        raise SystemExit("Run this installer in the Linux receiver's graphical session")
    if not os.environ.get("WAYLAND_DISPLAY"):
        raise SystemExit("Open a terminal inside the intended Wayland session before installing")
    for name in ("mpv", "systemctl", "ss"):
        if shutil.which(name) is None:
            raise SystemExit("Required command is missing: " + name)
    service = "mac-h264-display.service"
    subprocess.run(["systemctl", "--user", "show-environment"],
                   stdout=subprocess.DEVNULL, check=True)
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", service])
    if active.returncode == 0:
        raise SystemExit("Disconnect the active display before replacing the receiver")
    source = Path(__file__).resolve().parent
    home = Path.home()
    library = home / ".local/lib/mac-h264-display"
    state = home / ".local/state/mac-h264-display"
    units = home / ".config/systemd/user"
    sources = {library / "receiver.py": source / "receiver.py",
               library / "refinement.py": source.parent / "refinement/refinement.py",
               units / service: source / service}
    for target, origin in sources.items():
        if not origin.is_file():
            raise SystemExit("Missing source: " + str(origin))
        if target.is_symlink():
            raise SystemExit("Refusing to replace a symbolic link: " + str(target))
    for directory in (library, state, units):
        if directory.is_symlink():
            raise SystemExit("Refusing a symbolic-link installation directory: " + str(directory))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = library / "backups" / stamp
    backup.mkdir(mode=0o700, parents=True)
    launcher = library / "run-receiver"
    if launcher.is_symlink():
        raise SystemExit("Refusing to replace a symbolic-link launcher")
    for path in (*sources, launcher):
        if path.exists():
            shutil.copy2(path, backup / path.name)
    for target, origin in sources.items():
        shutil.copy2(origin, target)
        target.chmod(0o600 if target.suffix == ".service" else 0o700)
    python = str(Path(sys.executable).resolve())
    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(python) + " "
                        + shlex.quote(str(library / "receiver.py")) + ' "$@"\n')
    launcher.chmod(0o700)
    # Import only display-session values already supplied by this login. Never
    # guess a Wayland socket or force a particular GPU driver.
    names = [name for name in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP")
             if os.environ.get(name)]
    subprocess.run(["systemctl", "--user", "import-environment", *names], check=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print("Receiver installed; the service was not started or enabled. Backup: " + str(backup))


if __name__ == "__main__":
    main()
