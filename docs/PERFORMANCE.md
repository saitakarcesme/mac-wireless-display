# Performance and measurement limits

This report describes a prototype, not a guaranteed service level. Public results omit private hostnames, network identifiers, usernames, packet logs and screen captures.

## Tested system

- Apple M1 MacBook Air, 8 GB RAM, macOS 26.
- Older Intel Core i5-2520M laptop, HD Graphics 3000, `i965` VAAPI, Wayland/Hyprland.
- One native 1920 × 1080 external receiver monitor at 100 Hz.
- 120 Hz virtual macOS display; encoded video target 60 FPS, 18 Mbit/s.
- Wi-Fi-only direct AP/station arrangement on one 2.4 GHz radio.

These results are not transferable without measurement to a different GPU, network, driver, panel or compositor. Only one additional display was tested.

The public package generalizes the local prototype's app names, configuration and installers. Both public apps build, and the offline tests pass; a fresh installation across arbitrary Mac/Linux combinations has not been validated. The numbers below describe the tested prototype's streaming path, not a claim that every public installation reproduces them.

## Retained changes

Increasing virtual-display timing from 60 to 120 Hz restored source cadence from approximately 30 to 58 FPS on this system, while preserving a 60 FPS encoder target. Native receiver output, one-frame swapchain depth, VAAPI decoding and default compositor pacing reduced the receiver-local median to roughly 14–18 ms in measured cohorts. Settled RGB restoration and the normal connect/disconnect lifecycle were verified separately.

## Rejected timer experiment

A same-binary OFF/ON/OFF comparison changed only the high-precision submission timer. Each full-scroll stimulus lasted 45 seconds; presentation results used matched 39-second interior windows and included decoder recovery.

| Metric | Default timer A | Precise timer | Default timer B |
| --- | ---: | ---: | ---: |
| Host sent FPS | 57.139 | 59.658 | 57.167 |
| Actual presented FPS | 55.39 | 53.62 | 54.72 |
| Arrival → presentation median | 17.64 ms | 18.14 ms | 17.64 ms |
| Arrival → presentation p95 | 52.39 ms | 56.14 ms | 56.17 ms |
| Callback → presentation median | 43.29 ms | 44.92 ms | 43.78 ms |
| Callback → presentation p95 | 117.91 ms | 151.73 ms | 130.96 ms |
| Maximum presentation gap | 638.88 ms | 680.00 ms | 660.00 ms |
| Host + encoder + menu CPU | 23.98% | 25.14% | 23.46% |

CPU is a percentage of **one CPU core**, using matched motion-phase samples. Shared WindowServer CPU was about 47–50% of a core and is reported separately because other displays and applications also use it. No watts or battery-life measurement was made.

The precise timer improved sender cadence by 4.38%, but actual presentation did not improve in these network-variable cohorts. It stays disabled. All three trials encountered upstream stalls and decoder recovery, so this is not a clean causal claim that a precise timer makes the network worse.

## What the timestamps mean

- **Callback → presentation:** the host's ScreenCaptureKit callback entry to receiver compositor presentation feedback.
- **Arrival → presentation:** the Linux receiver's packet arrival to compositor presentation feedback.
- **Physical input-to-photon:** not measured. It also includes input processing, the source application's timing, scanout position and panel response.

Clock alignment uses repeated monotonic midpoint probes. Estimated clock uncertainty for the three cohorts was ±1.71, ±2.14 and ±1.83 ms, under a symmetric-path assumption. Capture modes can change when the callback arrives relative to the scheduled display time, so results from different timestamp boundaries must not be compared as physical latency improvements.

## Remaining issues

Occasional large keyframes and upstream delivery pauses still produce visible tail latency. One 292 KB IDR began its decoder write with roughly 61 ms of its receiver budget remaining and still timed out after a partial write. A small five-millisecond completion allowance cannot fix that event. Longer keyframe intervals require a proven immediate recovery protocol before they are safe to enable.

The tested system can feel responsive during ordinary use while failing a full-scroll stress target. Both observations matter. Do not infer zero latency, cable equivalence or near-zero total Mac load from average FPS or a short successful demo.
