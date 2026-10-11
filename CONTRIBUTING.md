# Contributing

Keep source comments, documentation, UI text, issue reports and pull requests in English.

Before proposing a performance change:

1. Preserve a working build and settings for rollback.
2. Test one independent variable at a time with the same workload.
3. Measure actual presentation, recovery events and tail latency, not only sender FPS.
4. Report clock uncertainty, CPU units, hardware and configuration.
5. Distinguish synthetic tests from live display evidence.
6. Restore the normal receiver, settings and desktop after the experiment.

Do not upload screen recordings, private hostnames, IP addresses, Wi-Fi profiles, keys, access tokens, signed application bundles, personal paths or raw runtime logs. Include a minimal generated reproduction and a redacted summary instead.

Run `python3 tools/run_tests.py` before submitting. On macOS also build the apps with `./tools/build_macos.sh`. Passing offline tests does not establish a latency or image-quality improvement.
