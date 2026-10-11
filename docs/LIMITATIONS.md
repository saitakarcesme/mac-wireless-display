# Known limitations

- One additional 1080p display is implemented. Multiple remote displays are untested and are not advertised as supported.
- The virtual-display interface is private macOS API and can change between OS releases.
- Wi-Fi airtime, retransmissions and large keyframe bursts can cause stalls. Zero visible delay is not guaranteed.
- Motion video uses lossy H.264 4:2:0. Lossless original RGB is available only when content settles; a blinking caret or continuous animation can prevent a full-screen snapshot from staying visible.
- Two physical panels can look different even when the software RGB values match. No physical color calibration was performed.
- Linux support currently targets a Wayland/mpv/VAAPI session. Windows, X11, software decoding and arbitrary GPU combinations are not validated.
- Session notifications, screensavers, locking and lid/suspend policy require explicit receiver-session setup.
- Building a new ad-hoc-signed host can trigger a new macOS screen recording grant. The software does not bypass that grant.
- The Mac must remain awake and logged into the required session. Disk encryption and login authentication remain in place after reboot.
- There is no audio, remote input injection, clipboard, file transfer or Internet relay.
- Local installation is source-based. No signed/notarized end-user binary or automatic updater is supplied.
