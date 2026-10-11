# Display panel

Click the menu-bar display symbol to open the native macOS popover. The panel shows the currently detected screens, their names and dimensions, and a scaled map of their actual desktop bounds. The wireless output uses the system accent color. Screen numbers are ordered by position, not permanent hardware identifiers.

The map is informational. **Display Settings…** opens macOS display settings for arrangement changes. **Connect / Disconnect** controls connection intent, including automatic retries. **Quit** stops the connection and exits. Escape or clicking outside closes the panel without ending the connection.

Adding a screen through this project still means the single tested wireless output. Showing several existing wired screens in the map does not imply multi-receiver streaming support.

## Receiver cursor

The Mac cursor is part of the captured video. The receiver player requests an always-hidden local pointer. If a compositor still leaves its local pointer visible, use that compositor's idle-cursor setting on the dedicated receiver.

For a current Hyprland Lua configuration, an example is:

```lua
hl.config({ cursor = { inactive_timeout = 0.5 } })
```

This hides the receiver pointer after half a second of local inactivity. Moving a ThinkPad mouse or touchpad can reveal it briefly again. It does not hide the Mac cursor embedded in the video. Keep a backup and validate the syntax against the installed compositor version; this is not a universal Linux setting.
