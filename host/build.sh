#!/bin/sh
set -eu
HOST_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$HOST_DIR")
APP_DIR="${DISPLAY_BUILD_DIR:-$REPO_DIR/build}/Wireless Display Host.app"
SIGN_IDENTITY="${DISPLAY_SIGN_IDENTITY:--}"
mkdir -p "$APP_DIR/Contents/MacOS"
cp "$HOST_DIR/Info.plist" "$APP_DIR/Contents/Info.plist"
xcrun swiftc -parse-as-library -O -framework AppKit -framework ScreenCaptureKit -framework VideoToolbox -framework CoreMedia -framework CoreVideo "$HOST_DIR/RecoveryControl.swift" "$HOST_DIR/RecoveryTransport.swift" "$HOST_DIR/SubmissionPacer.swift" "$HOST_DIR/KeyframeCadence.swift" "$HOST_DIR/LatestFrameGate.swift" "$HOST_DIR/StaticRefinement.swift" "$HOST_DIR/TileRefinement.swift" "$HOST_DIR/WirelessDisplayHost.swift" -o "$APP_DIR/Contents/MacOS/WirelessDisplayHost"
codesign --force --sign "$SIGN_IDENTITY" --identifier local.mac-wireless-display.host "$APP_DIR"
codesign --verify --deep --strict "$APP_DIR"
printf '%s\n' "$APP_DIR"
