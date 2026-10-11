# Linux session integration

The receiver runs inside your own Wayland login session. It requests fullscreen output and disables mpv's OSD, but cannot prevent another program or compositor from drawing over it.

For a dedicated display appliance, configure your compositor so that:

- The external output is native 1920 × 1080, scale 1, with the intended refresh rate.
- The receiver window is fullscreen on that output.
- The laptop's internal panel is disabled when appropriate.
- Panels, notification popups, lock screens, idle animations and screen savers do not interrupt the receiving session.
- A solid black background remains when the receiver disconnects.

The tested compositor was Hyprland. Configuration syntax differs between Hyprland versions and Omarchy releases, so this repository does not overwrite a user's compositor files. Use the documentation for your installed version. Keep a local terminal or alternate login available while changing output rules.

## Closed-lid operation

Review your distribution's `systemd-logind` lid settings and any desktop power manager that can independently suspend the machine. On an AC-powered dedicated receiver, the relevant `logind.conf` settings are `HandleLidSwitch=ignore`, `HandleLidSwitchExternalPower=ignore`, and `HandleLidSwitchDocked=ignore`. Apply them through a small configuration drop-in using your normal administrative workflow.

Do not assume a file edit proves the policy: close the lid, verify that the external monitor continues updating, and separately test reconnect after a reboot. A disk-encryption password or graphical-session login at boot still requires its normal authentication. This project does not remove those protections.

## Direct Wi-Fi

The tested setup eventually connected the Mac directly to an access point hosted by the Linux computer. This can avoid a poor path through a distant router. However, simultaneous AP and station operation depends on the Wi-Fi chipset, driver, supported interface combinations, and shared-channel airtime. It is not a universal performance fix.

This release deliberately includes no access-point password, network profile, private address, firewall change, or automatic network switch. Use your normal network-management tools and preserve a recovery route when experimenting.
