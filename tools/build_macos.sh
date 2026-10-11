#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$(uname -s)" != Darwin ]; then
  echo 'The macOS apps must be built on macOS with Xcode Command Line Tools.' >&2
  exit 1
fi
xcrun --find swiftc >/dev/null
export DISPLAY_BUILD_DIR=${DISPLAY_BUILD_DIR:-"$root/build"}
sh "$root/host/build.sh"
sh "$root/menu/build.sh"
echo 'Build complete. No app was installed or launched.'
