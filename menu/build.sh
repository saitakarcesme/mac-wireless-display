#!/bin/sh
set -eu
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
build_dir=${DISPLAY_BUILD_DIR:-"$source_dir/../build"}
app="$build_dir/Wireless Display.app"
mkdir -p "$app/Contents/MacOS"
cp "$source_dir/Info.plist" "$app/Contents/Info.plist"
xcrun swiftc -O -import-objc-header "$source_dir/CGVirtualDisplayPrivate.h" \
  -framework Cocoa -framework CoreGraphics "$source_dir/HostLaunchGate.swift" \
  "$source_dir/ConnectionPolicy.swift" "$source_dir/DisplayPanel.swift" "$source_dir/DisplayMenu.swift" -o "$app/Contents/MacOS/WirelessDisplayMenu"
codesign --force --sign "${DISPLAY_SIGN_IDENTITY:--}" --identifier local.mac-wireless-display.menu "$app"
codesign --verify --deep --strict "$app"
printf '%s\n' "$app"
