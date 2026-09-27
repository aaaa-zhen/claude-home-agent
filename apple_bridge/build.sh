#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SOURCE="$ROOT/apple_bridge/Sources/AppleBridge/main.swift"
INFO="$ROOT/apple_bridge/Info.plist"
OUTPUT_ROOT="$ROOT/runtime/apple-bridge"
APP="$OUTPUT_ROOT/AppleBridge.app"
MACOS="$APP/Contents/MacOS"

mkdir -p "$MACOS"
cp "$INFO" "$APP/Contents/Info.plist"
xcrun swiftc -O \
  -framework AppKit \
  -framework Carbon \
  -framework EventKit \
  "$SOURCE" \
  -o "$MACOS/AppleBridge"
codesign --force --sign - --identifier com.zhen.homeagent.applebridge "$APP" >/dev/null
echo "$APP"
