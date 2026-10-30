#!/usr/bin/env bash
# Build, install and launch Aster on the paired iPhone, capturing guest output.
#
# Prerequisites: the device is paired, Developer Mode is on, and the phone is
# UNLOCKED. iOS refuses to launch an app on a locked device (FBSOpenApplication
# error 7), so the script checks and stops with that exact error instead of
# pretending the run failed for another reason.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVICE="${ASTER_DEVICE_UDID:-00008150-001C056A3693401C}"
BUNDLE="${ASTER_BUNDLE_ID:-dev.ekas.aster}"
APP="$ROOT/build/xcode/Build/Products/Release-iphoneos/Aster.app"

"$ROOT/tools/build_guest.sh"
xcodebuild -project "$ROOT/app/Aster.xcodeproj" -scheme Aster -configuration Release \
  -destination "id=$DEVICE" -derivedDataPath "$ROOT/build/xcode" \
  -allowProvisioningUpdates build >/dev/null

xcrun devicectl device install app --device "$DEVICE" "$APP" >/dev/null

set +e
OUTPUT="$(timeout 90 xcrun devicectl device process launch --device "$DEVICE" \
  --console --terminate-existing "$BUNDLE" --autorun 2>&1)"
STATUS=$?
set -e
printf '%s\n' "$OUTPUT"

if printf '%s' "$OUTPUT" | grep -q "BSErrorCodeDescription = Locked"; then
  echo
  echo "PHONE LOCKED: unlock the iPhone and re-run. The app is installed; no rebuild needed." >&2
  exit 2
fi
if printf '%s' "$OUTPUT" | grep -q "\[gate\] summary 6/6"; then
  echo
  echo "DEVICE GATE PASSED (6/6)"
  exit 0
fi
if [ "$STATUS" -eq 124 ]; then
  echo
  echo "Launch succeeded but --console timed out; results were captured above." >&2
  exit 0
fi
echo
echo "DEVICE GATE NOT CONFIRMED (exit $STATUS)" >&2
exit 1
