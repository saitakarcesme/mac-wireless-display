# Connection recovery

The menu keeps an explicit connection intent. Once connected, it retries failures without an overall time limit, using a 2–30 second backoff. **Disconnect** and **Quit** cancel retries. Closing the panel does not disconnect. Intent is saved locally so a relaunched menu can resume a previously requested connection. OS termination preserves that saved intent; explicit Disconnect and the panel’s Quit clear it.

During recovery, the menu retains its virtual display object and preserves its arrangement. The video host and SSH tunnel are replaced only after their owned predecessors stop. A menu-process crash itself destroys its virtual display; retention applies to transport/host recovery while the menu is alive.

## What is checked

- The SSH tunnel uses keepalives to detect an unresponsive connection.
- Receiver telemetry must change at least once in 12 seconds. Comparing heartbeat tokens with a local monotonic clock avoids relying on synchronized machine clocks.
- An idle desktop can keep the same frame count indefinitely. This is healthy when telemetry and the renderer remain responsive.
- A renderer with no playback progress for more than 10 seconds is considered stuck only if at least four newer frames were written. A small final decoder tail is allowed.
- A missing renderer or stale telemetry triggers connection teardown and retry.
- A connection activity assertion prevents automatic idle system sleep on the Mac while connection intent is enabled. Disconnect and Quit release it. Explicit sleep, lid behavior, shutdown, and hardware failure remain outside this guarantee.

## Safe recovery after an extended outage

The helper records a random installation ID locally and an ownership marker on the receiver. The marker also identifies the connection attempt and the systemd service invocation. Claim and release operations are serialized with a remote file lock. This is not an authentication mechanism: SSH still requires verified host keys and unattended login.

When connectivity returns, the same installation can reclaim its abandoned receiver invocation even if stale status still says connected. A different client or a later unknown invocation is preserved. Cleanup from an older attempt cannot stop the replacement session. If ownership cannot be verified, the panel keeps retrying and reports the conflict rather than stealing another stream.

## Limits

Recovery is not uninterrupted transmission. The peer must be powered on, running its OS, reachable over the configured network, and able to authenticate. The generic helper does not switch Wi-Fi networks. A completely hung kernel or radio can require physical intervention. No software-only change here establishes that the earlier whole-machine freeze cannot recur.

Optional macOS login supervision can restart a crashed menu process. For a LaunchAgent configured to launch the installed menu, use `KeepAlive` with `SuccessfulExit: false` and `ThrottleInterval: 10`. A normal Quit exits successfully. This is separate from transport recovery and is not installed automatically by the generic installer.

## Validation

Offline tests exercise a full simulated day of idle telemetry, replayed health data, clock adjustments, decoder-generation changes, stalled playback, indefinite retry backoff, manual cancellation, stale-session reclamation, foreign-session preservation, and late cleanup.

In a local Wi-Fi test, terminating exactly the helper-owned SSH tunnel resulted in an automatic new video session in approximately 9 seconds, with the same virtual display ID. This is one recovery test, not an availability guarantee or a latency benchmark. See the repository's performance notes for the separate steady-state display measurements.

A second local fault test paused the invocation-verified receiver process with `SIGSTOP`, leaving its last status falsely connected. The monitor detected the stale heartbeat and recovered a fresh stream in approximately 24 seconds, again retaining the virtual display ID. This tests an application-process stall; it does not simulate or cure a frozen kernel. Manual Disconnect was also left idle beyond the maximum retry delay and did not reconnect.

With local LaunchAgent crash supervision enabled, killing the exact installed menu process recovered a new menu and video session in approximately 9 seconds. The virtual display is recreated in this case, as documented above. Normal Quit completed without restarting, and the saved connection intent was false.

## Unresolved receiver availability issue

A later unattended idle check did **not** pass: the receiver became unreachable through all available management paths, and its direct Wi-Fi access point disappeared. A bounded network wake attempt did not restore access. The cause has not been established; local inspection is required. The application remains configured to retry when the receiver becomes reachable again. The process-fault tests above must not be interpreted as proof of overnight availability or a fix for this machine-level outage.

### Congestion investigation (October 12)

A receiver can remain connected while its display stutters. Check competing traffic
as well as average frame rate. In one attended direct-Wi-Fi session, the source still
produced approximately 57 FPS, but RTT spikes reached 156–259 ms and cloud downloads
transferred 48–58 MB per 30-second observation. These observations identify congestion
as a candidate, not proof that every stall has the same cause.

An AF41 traffic-class experiment initially reduced receiver arrival-to-mpv-notification
p95/p99 from 32.66/63.35 ms in a control run to 14.35/30.06 ms. A repeat nevertheless
suffered decoder recovery and over 200 dropped frames. **The change was rolled back**;
the shipped tunnel still uses the system traffic-class default. These are software
notification timings, not physical input-to-photon measurements. Background traffic
was uncontrolled, so the comparison cannot establish a reliable QoS benefit.

For this display-only network, macOS **Low Data Mode** was then enabled in Wi-Fi →
Details. A subsequent 30-second observation no longer showed the large cloud-download
burst. The unchanged default-QoS stream completed the scrolling fixture with 1,755
frames received and written, zero drops, no decoder restart after initial launch,
and approximately 57 FPS during motion. Receiver notification p95/p99 was
29.33/53.80 ms. This supports keeping the network-specific setting for this installation,
but is not a controlled proof of cable-equivalent smoothness or overnight reliability.

[Apple documents Low Data Mode as a per-network data-use setting](https://support.apple.com/guide/mac-help/wi-fi-settings-on-mac-mh11935/mac).
It may defer background synchronization while connected to that network. It does not
change this project's encoder resolution, bitrate, or color settings. Other Wi-Fi
networks retain their existing settings; turn it off in that network's Details pane
to restore the previous behavior.
