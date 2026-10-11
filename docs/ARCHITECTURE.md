# Architecture

## Components

| Directory | Responsibility |
| --- | --- |
| `menu/` | Native macOS status item, virtual display lifetime, session ownership and layout. |
| `host/` | ScreenCaptureKit capture, VideoToolbox encoding, bounded latest-frame admission and settled RGB. |
| `receiver/` | Loopback access-unit listener, bounded decoder input and mpv lifecycle. |
| `refinement/` | Original RGB snapshot protocol and renderer synchronization. |
| `tools/` | Local build, installation and generated/offline tests. |

## Video path

The menu owns one virtual 1920 × 1080 display. The host captures that display with ScreenCaptureKit, requests hardware H.264 encoding, disables frame reordering, and explicitly schedules IDR frames. Only one frame is actively encoding/writing and one newest raw frame can be pending. Idle callbacks do not repeatedly encode an unchanged screen.

Video uses a 32-byte big-endian header: `>4sHHIQQI`. It carries the magic `H264`, protocol version, IDR flag, Annex-B payload length, sequence, callback monotonic timestamp and a reserved field. A complete IDR contains SPS/PPS. A version-2 dirty-mask extension exists for an experimental tile path, which is off by default.

The host connects to a Mac loopback port forwarded by SSH to the Linux loopback receiver. The receiver rejects oversized or malformed access units. If an already-partially-written access unit misses its deadline, the decoder must be replaced; appending another frame cannot repair a truncated byte stream.

## Sharp settled images

H.264 is efficient for motion, but 4:2:0 loses chroma detail in thin colored text. After the display settles, the host captures its **original sRGB BGRA pixels**, compresses them losslessly, and sends a bounded snapshot on a second loopback-forwarded channel. It never reconstructs the supposed original RGB from a decoded NV12 frame.

The receiver validates dimensions, epoch, sequence, compressed size and exact decoded size. A new video frame invalidates an old snapshot before it is forwarded. The mpv overlay acknowledgement is an ordering barrier, not evidence of physical monitor scanout. Tile refinement is source-level experimental work, not an enabled or fully validated feature.

## Color

The tested motion path uses limited-range NV12 with BT.709 primaries/matrix and sRGB transfer metadata. The target renderer outputs full-range RGB with sRGB transfer. Settled original RGB avoids motion-codec chroma loss. These metadata and protocol checks do not calibrate two different physical monitor panels.

## Ownership and security

The menu's helper tracks its own host executable/PID/start time, SSH subprocess and receiver session. It refuses an active receiver rather than stealing it. Closing the parent input or sending `STOP` initiates cleanup. Port preflight tolerates closed `TIME_WAIT` sockets while refusing a live listener.

Both video and refinement bind only to `127.0.0.1`. SSH provides encryption and peer authentication, with strict host-key checking and no automatic unknown-key acceptance. The payload protocols do not replace SSH authentication. Local processes under the same user are part of the trust boundary; this is not designed as a public multi-tenant streaming server.

The app does not create a screen-recording archive or send frames to a project server. The Linux receiver temporarily writes original BGRA pixels to mode `0600` files inside a mode `0700` temporary directory so mpv can load an RGB overlay. Normal processing unlinks each file after mpv acknowledges the load or the operation fails. Abrupt process termination can leave temporary pixel files behind; these are sensitive local screen content. Runtime files and local logs are not part of the public repository.
