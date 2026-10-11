#!/usr/bin/env python3
"""Run offline tests; never launch the display apps or contact another computer."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(arguments, **kwargs):
    subprocess.run([str(x) for x in arguments], cwd=ROOT, check=True, **kwargs)


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(str(ROOT / path) for path in ("menu", "receiver", "refinement", "tools"))
    for directory in ("menu", "receiver", "refinement", "tests"):
        run([sys.executable, "-m", "unittest", "discover", "-s", ROOT / directory, "-p", "test_*.py"], env=environment)
    if sys.platform == "darwin":
        suites = [
            ("launch-gate", ["menu/HostLaunchGate.swift", "menu/LaunchGateTests.swift"], []),
            ("latest-frame", ["host/LatestFrameGate.swift", "host/LatestFrameGateTests.swift"], []),
            ("keyframes", ["host/KeyframeCadence.swift", "host/KeyframeCadenceTests.swift"], []),
            ("recovery-control", ["host/RecoveryControl.swift", "host/KeyframeCadence.swift", "host/RecoveryControlTests.swift"], []),
            ("pacer", ["host/LatestFrameGate.swift", "host/SubmissionPacer.swift", "host/SubmissionPacerTests.swift"], []),
            ("tiles", ["host/StaticRefinement.swift", "host/TileRefinement.swift", "host/TileRefinementTests.swift"],
             ["AppKit", "ScreenCaptureKit", "CoreGraphics", "CoreVideo"]),
        ]
        with tempfile.TemporaryDirectory(prefix="wireless-display-tests-") as temporary:
            for name, sources, frameworks in suites:
                binary = Path(temporary) / name
                command = ["xcrun", "swiftc", "-parse-as-library", *sources, "-o", binary]
                for framework in frameworks:
                    command.extend(["-framework", framework])
                run(command)
                run([binary])
            # Exercise the actual Swift transport and Python receiver with
            # synthetic bytes on an ephemeral loopback port. No capture or decoder.
            transport_probe = Path(temporary) / "recovery-transport"
            run(["xcrun", "swiftc", "-parse-as-library",
                 "host/RecoveryControl.swift", "host/RecoveryTransport.swift",
                 "host/KeyframeCadence.swift", "tests/RecoveryTransportProbe.swift",
                 "-o", transport_probe])
            run([sys.executable, "tests/recovery_transport_integration.py", transport_probe,
                 "--output", Path(temporary) / "recovery-transport-results.json"], env=environment)
    print("Offline tests passed. No apps were installed or launched.")


if __name__ == "__main__":
    main()
