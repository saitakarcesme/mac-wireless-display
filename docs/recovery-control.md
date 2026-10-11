# Optional keyframe recovery

This extension is experimental and disabled by default. It lets the receiver
request a fresh H264 keyframe after losing the decoder reference chain. It
uses the reverse direction of the existing SSH-forwarded video connection;
there is no additional network listener or credential.

The default remains one keyframe every 30 submitted frames. A longer configured
interval is used only after the receiver explicitly accepts the extension.
Reducing keyframe frequency reduces large refresh bursts; it does not reduce
individual keyframe size, remove Wi-Fi pauses, or guarantee monitor-like latency.

## Configuration

Keep both endpoints on compatible versions. For a first recovery test, merge
these fields into `~/.config/mac-wireless-display/h264-settings.json`:

```json
{
  "recoveryControl": true,
  "keyframeInterval": 30
}
```

Start the compatible receiver with `--recovery-control`. Verify normal video
and recovery after restarting only the owned decoder while the desktop is
static. A longer interval, such as 120, should be an explicit experiment only
after that succeeds. All other resolution, bitrate, color, frame-rate, and
capture settings should remain unchanged for a meaningful comparison.

The host also supports `--recovery-control true|false` and
`--keyframe-interval 30..600`. Turning recovery off keeps the effective interval
at 30 even if a longer interval remains in the settings. Remove the receiver
flag for the original receive behavior. These options do not grant capture
permission; the normal operating-system permission is still required.

## Protocol and bounds

Control records are exactly 32 bytes: big-endian `>4sBBHQQQ`, with magic `HCTL`,
version 1, opcode, zero reserved bits, a random nonzero connection epoch,
request ID, and video sequence. Opcodes are HELLO, ACCEPT, REQUEST_IDR,
IDR_SENT, and DECLINE. Negotiation uses request ID zero and `UINT64_MAX` as
its sequence sentinel. The full specification is maintained with the receiver.

Epoch, direction, record fields, increasing request IDs, and future sequences
are checked. The host buffers one control record and coalesces requests. Forced
recovery submissions are separated by at least 250 ms. A started partial record
or write has a 250 ms decision deadline; negotiation has a 1 second deadline.
A previous socket write and operating-system scheduling can delay these
handlers. They are software bounds, not hard real-time delivery guarantees.

IDR_SENT only reports that the host completed writing a named keyframe. The
receiver remains in recovery until a complete keyframe is actually written to
the decoder. An acknowledgment never authorizes decoding dependent frames on
its own. Video sequence numbers and H264 v1/v2 framing are unchanged.

A compatible receiver with recovery disabled replies DECLINE and continues
ordinary video. An older receiver may reject HELLO. The host then reconnects
with control disabled for that process and retains the 30-frame fallback. That
first opt-in compatibility attempt can interrupt presentation.

## Static-source safety

During motion, the request forces the next retained raw frame. With no raw
frame available, the host makes one on-demand capture using the exact same
display filter, dimensions, cursor and color settings. Newer ordinary capture
input supersedes that screenshot. Epoch and generation checks prevent a delayed
screenshot from overwriting newer pixels. Normal motion remains zero-copy when
that mode is selected; the exceptional idle screenshot uses a bounded copy pool.

At most one physical screenshot call is outstanding. A timeout invalidates its
result but retains that slot until the framework callback returns. This avoids
unbounded API calls; a permanently hung call can leave idle recovery unavailable
until it returns or the host restarts. Normal moving capture remains separate.

## Verification and limits

Host tests cover record validation, epoch/generation checks, request coalescing,
rate bounds, canceled-timer identity, newer-frame precedence, and idle recovery
admission. Cross-language tests exercise the actual Swift transport and Python
receiver on loopback. Hardware-generated frames separately verify that Apple's
encoder emits a requested keyframe, including after simulated idle admission.

A generated scrolling text/color/stroke comparison checked every decoded frame
against its exact pre-encode NV12 input. A longer interval did not show a material
codec-quality penalty in that scene. This is not a universal quality guarantee,
an RGB 4:4:4 reference, a panel-color test, or a physical input-to-display latency
measurement. The path still uses 4:2:0 H264 during motion. Real network stalls and
decoder restarts remain possible.

To reproduce the offline tests, run `python3 tools/run_tests.py`. On macOS,
the runner also builds the real Swift transport fixture and exercises it
against the Python receiver using an ephemeral 127.0.0.1 port and generated
NAL-like bytes. Its ten cases cover negotiation, requested recovery, disabled
compatibility, a legacy peer, and malformed, replayed, flooded or incomplete
control messages. The fixture neither captures a display nor runs a decoder;
its temporary results are removed when the test runner exits. Linux runs the
Python policy tests and skips the macOS-only Swift fixture.
