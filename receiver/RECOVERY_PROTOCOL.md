# Optional immediate keyframe recovery

This opt-in extension is disabled by default. It asks the host for a fresh
keyframe after reference loss or decoder exit, including when the source was
idle. No performance claim follows from the protocol tests alone.

## Transport and compatibility

Control uses the existing full-duplex video TCP connection. Both endpoints bind
only to loopback, and the external connection is the user's authenticated,
host-key-pinned SSH tunnel. There is no additional listener or credential.
The host must explicitly enable recovery control. A receiver without that
option still recognizes HELLO, replies DECLINE, and accepts ordinary video.
A receiver never sends control before HELLO, even if its option is enabled.

The host keeps the ordinary 30-frame keyframe interval until ACCEPT is fully
received. A legacy receiver may reject HELLO: the host then reconnects once,
disables control for the remainder of that process, and retains the 30-frame
fallback. A negotiation timeout has the same host fallback. Existing H264 v1
and v2 video headers and idle RGB refinement are unchanged.

## Fixed record

Every control record is exactly 32 bytes in network byte order:

| Field | Type | Value |
| --- | --- | --- |
| Magic | 4 bytes | `HCTL` |
| Version | uint8 | `1` |
| Opcode | uint8 | See below |
| Reserved | uint16 | `0` |
| Epoch | uint64 | Random nonzero value chosen by host for this connection |
| Request ID | uint64 | Negotiation: zero; recovery: monotonically increasing from one |
| Sequence | uint64 | Video AU sequence, or `UINT64_MAX` sentinel |

Python struct format: `>4sBBHQQQ`. Magic distinguishes control from an ordinary
32-byte H264 header. No control bytes occur inside a video access unit.

| Opcode | Direction | Meaning |
| --- | --- | --- |
| 1 HELLO | Host → receiver | Offer extension before the first video AU |
| 2 ACCEPT | Receiver → host | Enable extension for this epoch |
| 3 REQUEST_IDR | Receiver → host | Recover reference chain; sequence is last fully received validated AU, or sentinel |
| 4 IDR_SENT | Host → receiver | Echo request ID and actual fully written IDR sequence |
| 5 DECLINE | Receiver → host | Continue ordinary video without extension |

HELLO, ACCEPT and DECLINE require request ID zero and the sequence sentinel.
The host rejects a request naming a future AU. The receiver rejects the wrong
epoch, wrong direction, duplicate/unknown acknowledgment, or acknowledgment
that does not name a received IDR newer than the request's sequence. An ACK may
arrive after that IDR was already written to the decoder. It remains telemetry:
**only successfully writing a complete IDR to the decoder clears recovery**.
The receiver additionally checks that a declared IDR has an Annex-B type-5 NAL.

## Bounded recovery

The existing 100 ms queue-age rule, 8 MiB/120-frame queue capacity, and
complete-write-or-decoder-reset invariant remain intact. There is no write
completion grace in this experiment. A dropped complete AU invalidates the
reference chain and requests an IDR; subsequent P frames are discarded until
an IDR. A partial decoder-pipe write still terminates the decoder. When recovery is
enabled, an empty queue uses a 250 ms condition wait so an exited decoder is
detected even while the desktop is static. Once negotiation is active, that
exit triggers one coalesced recovery request; no animation is required.

Queue loss only signals a controller; it never performs network I/O while
holding the queue lock. One reverse writer owns a duplicate socket descriptor,
so an old worker cannot send into a newly reused connection descriptor. It
keeps one coalesced pending recovery and at most 16 request records, 64 received
IDR sequences and 16 recent incoming-control timestamps. A new recovery request
is separated from the preceding request by at least 250 ms. While the same
recovery remains unresolved, retries occur no more than once per second.

A recovery barrier records the latest received sequence at invalidation. An
older in-flight IDR finishing after a newer loss cannot clear that recovery.
Disconnect discards all state; reconnect requires a fresh epoch and negotiation.
A lost ACK does not prevent recovery when the IDR arrives and is fully written.

The receiver bounds a started fixed header to 250 ms, including a partial H264
header. An entirely idle stream has no header deadline. A control write has a
250 ms decision deadline, and negotiation response has a one-second outer
budget. Failed or partial reverse writes close the connection. These are
software deadlines, not hard real-time scheduling guarantees. The receiver allows at most 16 control records in a rolling second. The host
allows 16 in each fixed one-second window (up to 32 may straddle a boundary);
its separate 250 ms IDR submission interval still limits recovery amplification.

The host must supply a fresh IDR even when the source is static. It may use a
single on-demand capture with connection/generation checks and newer-frame
supersession. Merely waiting for another screen-change callback is insufficient.
Receiver tests exercise this contract using a synthetic host. A coordinated
real integration recovered from an initially idle source after the owned player
exited: an on-demand capture triggered fresh screen-capture input, which
superseded the pending screenshot. The accepted screenshot-copy branch remains
a separate validation requirement; successful newer-frame supersession does not
prove that branch.

## Offline verification

Run from `receiver/`:

```sh
python3 -m unittest test_receiver test_recovery
```

The tests use local socketpairs and synthetic AU bytes, with no display,
decoder, service change or remote listener. They cover protocol validation,
wrong epochs/ACKs, rate and memory bounds, partial fixed headers and reverse
writes, descriptor lifetime, queue coalescing, reference gaps, and recovering a
paused synthetic source after a sequence gap, a partial decoder write, and an
idle decoder exit with no intervening video frame.
These unit tests do not establish a reduction in user-visible latency, validate
actual VideoToolbox IDR emission, or validate the host's idle capture implementation.

## Enable deliberately

The receiver command accepts `--recovery-control`. The host must also opt in
with `recoveryControl: true` in its settings; neither side sends unsolicited
control to a legacy peer. Keep the default 30-frame keyframe interval until the
negotiated path and idle recovery have been validated for the target devices.
A longer `keyframeInterval` is effective only after successful negotiation.

For a manually managed user service, append `--recovery-control` to its existing
receiver command, preserving the configured ports, renderer and refinement
arguments. Then reload the user service manager and reconnect. To disable,
remove the option and reconnect; the receiver declines negotiation and the host
uses the 30-frame fallback. There is no additional port or credential to set up.
