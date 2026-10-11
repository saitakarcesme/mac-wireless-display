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
    print("Offline tests passed. No apps were installed or launched.")


if __name__ == "__main__":
    main()
