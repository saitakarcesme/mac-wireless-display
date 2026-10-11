# Setup

This is a developer preview. The source was exercised on one Apple M1 MacBook Air and one older Intel Linux laptop. The installer does not configure networking, authorize SSH, change login settings, suppress notifications, or grant screen recording permission for you.

## Requirements

| Mac | Linux receiver |
| --- | --- |
| macOS 15 or newer; tested on macOS 26 | A running Wayland graphical session |
| Xcode Command Line Tools | Python 3.10 or newer |
| Python 3.10 or newer | mpv with VAAPI hardware decoding |
| Hardware H.264 VideoToolbox encoder | A working H.264 VAAPI driver and access to the render device |
| Screen recording permission | SSH server and a user systemd manager |

Both machines must reach each other on a trusted network. Keep the Linux computer connected to its external monitor. A native 1920 × 1080 output at scale 1 is the tested receiver geometry. Other sizes, desktop scaling, GPU drivers, and compositors need validation.

The macOS virtual display uses the undocumented `CGVirtualDisplay` API. An OS update can break it. This project is not suitable for an App Store distribution that requires public APIs only.

## 1. Prepare the receiver

Install Python, OpenSSH, mpv and the VAAPI driver appropriate for the Linux GPU through your distribution's package manager. On an older Intel Sandy Bridge machine the tested driver is `i965`; newer Intel hardware generally uses a different driver. Do not force `i965` on an unrelated GPU.

Clone this repository on Linux. From a terminal **inside the graphical session**, run:

```sh
./receiver/install.sh
systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR
```

The installer registers the service but does not start or enable it. The Mac's connection helper starts and stops this service for its own session. If a driver override is necessary, use a user service drop-in and your tested driver value:

```ini
# ~/.config/systemd/user/mac-h264-display.service.d/gpu.conf
[Service]
Environment=LIBVA_DRIVER_NAME=i965
```

Run `systemctl --user daemon-reload` after changing a drop-in. Verify your driver using your distribution's VAAPI diagnostic tools before attempting a stream. The receiver deliberately refuses software decoding fallback.

## 2. Establish SSH from the Mac

Configure an SSH alias in `~/.ssh/config`, replacing the example values locally:

```sshconfig
Host display-receiver
    HostName receiver-hostname.local
    User your-linux-user
    IdentityFile ~/.ssh/your-existing-key
```

Make a normal first SSH connection and verify the receiver's host-key fingerprint using a trusted local source. Then verify unattended authentication:

```sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes display-receiver true
```

The app does not accept unknown keys or copy credentials. Use your existing SSH agent or a locally managed key; never add private keys or a populated local configuration to this repository. No Tailscale connection is required for the video path. An SSH destination reachable through Tailscale can still be used if that is your preferred network.

## 3. Build and install the Mac apps

With Xcode Command Line Tools and Python available:

```sh
./tools/build_macos.sh
python3 tools/install_macos.py --ssh-host display-receiver
```

Builds stay in `build/`. Installation copies the menu app to `~/Applications/Wireless Display.app` and its host/helper/configuration to `~/.config/mac-wireless-display/`. Existing installation files are backed up. The script refuses installation while either app is running.

By default the host is ad-hoc signed. macOS may require a fresh permission grant after a rebuilt ad-hoc app. For a stable local identity, set `DISPLAY_SIGN_IDENTITY` to **your own existing** signing identity before building. Never export a certificate or private key for this project. No signing identity or notarized binary is supplied.

Launch **Wireless Display** from Applications and choose **Connect**. When macOS requests screen recording access, authorize **Wireless Display Host** through System Settings. You may need to disconnect and reconnect after permission is granted. No Accessibility permission is required for this screen-only design; input stays on the Mac.

## 4. Arrange the desktop

Use macOS **System Settings → Displays → Arrange** to position the virtual display beside your physical one. The menu's **Arrange displays** action places the virtual display to the right of the main display without moving the other displays. The default virtual timing is 120 Hz while encoded output targets 60 FPS; the higher virtual timing was needed to keep ScreenCaptureKit cadence healthy on the tested Mac.

The Linux monitor should show only the received Mac display. Fullscreen mpv hides its own controls, but notification banners, screen locking, compositor panels and laptop-lid behavior are properties of your Linux session. Review [Linux session integration](LINUX-SESSION.md) before leaving a laptop closed. They are not disabled globally by the installer.

## Optional keyframe recovery

Recovery control is off by default. To enable it, disconnect first, then run this from the repository in the Linux graphical session:

```sh
./receiver/install.sh --recovery-control
```

The installer backs up the current files and registers the receiver flag; it does not start the service. On the Mac, merge these fields into `~/.config/mac-wireless-display/h264-settings.json`, preserving the other settings:

```json
{
  "recoveryControl": true,
  "keyframeInterval": 30
}
```

Reconnect from the menu and verify normal video and recovery from an idle source before experimenting with a longer keyframe interval. The host uses an interval above 30 only after the receiver accepts recovery control. The extension uses the existing SSH connection and requires no additional port, key or permission. See [keyframe recovery](recovery-control.md) for compatibility and validation limits, and the [wire protocol](../receiver/RECOVERY_PROTOCOL.md) for implementation details.

To return to the default, disconnect, rerun `./receiver/install.sh` without the flag, set `recoveryControl` to `false` in the Mac settings, then reconnect. The host returns to the 30-frame fallback. Existing custom systemd drop-ins can override the installer’s service command; inspect them if the effective arguments differ from your selected mode.

## Connection and cleanup

- **Disconnect** stops the app-owned stream and receiver, removes the virtual display, and preserves your Wi-Fi connection.
- Closing the status window keeps the connection active.
- Quit disconnects first. Do not force-kill the menu app during a connection unless normal shutdown fails.
- No login launch agent is installed automatically. You can add the menu app to Login Items yourself.

For diagnostics, inspect local files under `~/.config/mac-wireless-display/` on the Mac and `~/.local/state/mac-h264-display/` on Linux. Logs and configuration can contain private machine information; redact them before sharing.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Receiver unreachable | Run the exact SSH command above from the Mac; check Wi-Fi and the SSH alias. |
| Screen recording error | Reauthorize the installed host through macOS, then reconnect. Do not edit the TCC database. |
| Black output or decoder exit | Read the Linux user service log; verify hardware decoding and Wayland session variables. |
| Blurry small colored text | Wait for the settled RGB snapshot; moving H.264 uses 4:2:0 chroma subsampling. |
| Periodic large stalls | Check signal quality and local congestion; do not judge the link by Internet download speed alone. |
| Old Linux desktop appears | Configure the dedicated session's background/panels/notifications as described below. |
