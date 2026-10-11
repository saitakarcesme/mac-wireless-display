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

## Negotiated recovery and longer keyframe interval

A later same-binary A/B/A comparison kept the hardware encoder, 18 Mbit/s target, capture timing, lossless settled-image path and recovery protocol fixed. It changed only the requested keyframe interval: 30, 120, then 30 frames. Each stimulus was the same generated 45-second scrolling scene. Presentation statistics below cover the **whole 45 seconds**, including recovery; they must not be directly ranked against the shorter interior windows in the timer experiment below.

| Metric | 30 frames A | 120 frames | 30 frames B |
| --- | ---: | ---: | ---: |
| Actual presented FPS | 55.841 | 55.241 | 55.024 |
| Callback → presentation median | 38.796 ms | 27.974 ms | 33.866 ms |
| Callback → presentation p95 | 111.608 ms | 75.495 ms | 115.428 ms |
| Arrival → presentation median | 18.266 ms | 14.994 ms | 17.751 ms |
| Arrival → presentation p95 | 52.895 ms | 37.451 ms | 52.506 ms |
| Presentation gaps over 100 ms | 25 | 11 | 30 |
| Maximum presentation gap | 560.000 ms | 719.211 ms | 477.238 ms |
| Delivered video bitrate | 13.078 Mbit/s | 4.696 Mbit/s | 10.834 Mbit/s |
| Delivered IDR frames | 85 | 22 | 84 |
| Host + encoder + menu CPU | 21.109% | 20.988% | 21.445% |

The longer interval reduced typical software latency and the frequency of long gaps in this comparison, without improving delivered FPS. It also had the worst individual gap, during early recovery. A single A/B/A on variable Wi-Fi does **not** prove universal latency or reliability improvement. The stream's target bitrate is not a fixed wire rate: content and encoder rate control affect actual output.

CPU uses matched 31-sample motion windows and is a percentage of one core. This subtotal excludes the Python connection helper and SSH tunnel, so it is not whole-application CPU. The approximately 1% relative difference is too small to claim an efficiency gain. Shared WindowServer CPU was 50.741%, 51.338% and 46.927% of one core, separately. Estimated clock-alignment uncertainty was ±1.573, ±2.031 and ±2.112 ms, assuming a symmetric path.

The tested local profile retains the 120-frame interval with negotiated recovery. The public host defaults to a conservative 30-frame interval and recovery disabled; opt-in instructions are in [recovery control](recovery-control.md). If the peer does not negotiate, the host keeps the short interval.

### Recovery while the source is idle

A controlled fault stopped only the receiver's owned decoder process while the source was settled. The receiver requested a fresh IDR over the existing authenticated connection. A replacement decoder was observed about 588 ms after the fault; safe lossless RGB returned after about 2.68 seconds, including snapshot transfer. One recovery request and one replacement were recorded, without reconnecting the video TCP stream.

The screenshot request woke a newer ordinary capture callback, which correctly superseded the requested screenshot. This verifies idle recovery and stale-snapshot rejection in that case; it does not validate every screenshot fallback branch or guarantee recovery time on another system.

### Generated temporal quality check

A separate 360-frame sequence of moving text, colors and fine strokes compared intervals 30 and 120. Both used matching native 1080p NV12 reference frames, 18 Mbit/s hardware encoding and the same color metadata. Every frame decoded. Actual IDRs were at 0, 30, …, 330 versus 0, 120 and 240.

| Metric | Interval 30 | Interval 120 |
| --- | ---: | ---: |
| Median Y / U / V PSNR | 52.802 / 56.314 / 55.686 dB | 53.295 / 56.444 / 55.730 dB |
| Median edge Y / U / V absolute error | 0.758 / 0.690 / 0.741 | 0.701 / 0.668 / 0.719 |
| Actual encoded bitrate | 7.824 Mbit/s | 2.641 Mbit/s |

Edge errors are 8-bit code values. The worst paired edge-error increases were 0.036 / 0.022 / 0.022. This fixture found no material quality regression, but it is not a universal perceptual-quality result. Comparing against NV12 isolates encoding error; it does not measure the earlier loss of chroma detail when RGB becomes 4:2:0, physical panel colors, Wi-Fi delivery or Mac power consumption.

## Unadopted decoder-write allowance

A separate 0 / 10 / 0 ms completion-floor comparison tested whether a nearly complete frame could safely finish rather than reset its decoder. All three 45-second trials had zero dropped frames and no decoder replacements. The 10 ms allowance was **never exercised**, so apparent differences between those runs cannot be attributed to it. The exact pre-experiment receiver was restored, and the allowance is not included in this release.

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

Clock alignment uses repeated monotonic midpoint probes. Estimated clock uncertainty for the timer cohorts was ±1.71, ±2.14 and ±1.83 ms, under a symmetric-path assumption. Capture modes can change when the callback arrives relative to the scheduled display time, so results from different timestamp boundaries must not be compared as physical latency improvements.

## Repeated motion and settled-image transitions

A five-minute correctness run alternated two seconds of motion and two seconds of still content, using the same recovery-aware local profile. All 150 phase markers were present. All 75 static phases received an accepted original RGB snapshot, and all 74 complete static-to-next-motion transitions removed the preceding overlay before a newer video access unit was written. The initial motion phase cleared a pre-run snapshot; four other snapshots were already cleared by a valid video header before their motion phase began. These cases are counted explicitly rather than presented as negative transition latency.

The end snapshot reported 8,600 received and written access units, no dropped units, one initial decoder and no recovery requests. RGB software acceptance occurred a median 655 ms after the static phase marker (95th percentile 696 ms, maximum 875 ms), with estimated clock-join uncertainty of ±3.645 ms under a symmetric-path assumption. The audit observes renderer acknowledgements and pipe-write ordering; it does not establish when physical panel pixels changed.

Across the matched 290-second mixed motion/static resource window, host + encoder + menu CPU averaged 10.752% of one core. Shared WindowServer averaged 41.873% of one core, separately. The Python connection helper and SSH tunnel are excluded from this subtotal. Diagnostic overhead is included. This mixed workload must not be compared directly with continuous-scroll CPU or FPS; still content intentionally stops producing video frames. Temporary instrumentation was removed and the ordinary connection was verified afterward.

## Ten-minute mostly settled connection

A subsequent 600-second ordinary-connection check used 21 process snapshots and six receiver-status samples. All samples stayed connected with a safe accepted RGB image matching the latest video sequence. Received and written frames both advanced by 182, with no drops or extra decoder starts; this was mostly settled content, not a completely frame-free idle.

Host + encoder + menu averaged 0.792% of one CPU core. This subtotal excludes the Python connection helper and SSH tunnel. Shared WindowServer averaged 30.740% separately. The three sampled processes retained stable PIDs; their resident-memory endpoint changes were −3.000, +0.609 and −0.031 MiB respectively. This short observation found no sustained growth in those endpoints, but it is not a proof against all memory leaks or a measurement of total graphics memory, watts or battery life.

## Remaining issues

### Optional encoder rate-window requests

A generated-only test encoded the same 360-frame native 1080p sequence in four fresh hardware RTVC sessions at an 18 Mbit/s target and a 120-frame keyframe interval. Requested `DataRateLimits` were 337,500 bytes per 100 ms, 225,000 bytes per 100 ms, 168,750 bytes per 50 ms, then the first request again. Every setter succeeded, but property readback returned `kVTPropertyNotSupportedErr` before and after encoding. All four encoded streams were byte-for-byte identical, with IDRs still reaching 266,153 bytes and a six-frame decode-time window reaching 379,772 bytes.

These measurements reject tighter requests as a useful burst-reduction control on this tested backend. The host's optional request is not an enforced cap, and setter success alone is not evidence of one. This experiment used generated frames; it measured neither network delivery nor physical display latency.

### Delivery pauses

Occasional large keyframes and upstream delivery pauses still produce visible tail latency. One 292 KB IDR began its decoder write with roughly 61 ms of its receiver budget remaining and still timed out after a partial write. A small five-millisecond completion allowance cannot fix that event. Negotiated recovery now permits a longer interval in the tested local profile, but it does not shrink each large IDR or eliminate network pauses. The public default remains conservative.

The tested system can feel responsive during ordinary use while failing a full-scroll stress target. Both observations matter. Do not infer zero latency, cable equivalence or near-zero total Mac load from average FPS or a short successful demo.
