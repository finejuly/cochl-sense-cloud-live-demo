#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${1:-$ROOT/CochlSenseCloudLiveDemo.app}"
PLIST="$APP_DIR/Contents/Info.plist"
EXECUTABLE="$APP_DIR/Contents/MacOS/CochlSenseCloudLiveDemo"
EXPECTED_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
EXPECTED_ARCHS="${COCHL_EXPECTED_MACOS_ARCHS:-}"

for command_name in codesign plutil lipo otool; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name was not found; run this verifier on macOS with Xcode Command Line Tools."
    exit 1
  fi
done

if [[ ! -f "$PLIST" || ! -x "$EXECUTABLE" ]]; then
  echo "Incomplete app bundle: $APP_DIR"
  exit 1
fi

plutil -lint "$PLIST" >/dev/null
PLIST_TARGET="$(plutil -extract LSMinimumSystemVersion raw "$PLIST")"
PLIST_IDENTIFIER="$(plutil -extract CFBundleIdentifier raw "$PLIST")"
if [[ "$PLIST_TARGET" != "$EXPECTED_TARGET" ]]; then
  echo "Info.plist requires macOS $PLIST_TARGET; expected $EXPECTED_TARGET."
  exit 1
fi

# Finder/iCloud File Provider can attach com.apple.FinderInfo and provenance
# xattrs after signing. They do not change sealed code/resources, but
# `codesign --strict` rejects them, so verify the signature using the same
# integrity rule macOS uses to launch the app.
if ! codesign --verify --deep --verbose=2 "$APP_DIR"; then
  echo "The app bundle does not have one valid code signature: $APP_DIR"
  exit 1
fi
SIGNATURE_DETAILS="$(codesign --display --verbose=4 "$APP_DIR" 2>&1)"
if [[ "$SIGNATURE_DETAILS" != *"Identifier=$PLIST_IDENTIFIER"* ]]; then
  echo "The code-signing identifier does not match Info.plist ($PLIST_IDENTIFIER)."
  exit 1
fi

typeset -a ACTUAL_ARCHS
ACTUAL_ARCHS=(${(z)$(lipo -archs "$EXECUTABLE")})
if (( ${#ACTUAL_ARCHS[@]} == 0 )); then
  echo "No Mach-O architectures were found in $EXECUTABLE."
  exit 1
fi

for arch in ${(z)EXPECTED_ARCHS}; do
  if (( ${ACTUAL_ARCHS[(Ie)$arch]} == 0 )); then
    echo "Expected architecture $arch is missing; found: ${ACTUAL_ARCHS[*]}"
    exit 1
  fi
done

for arch in "${ACTUAL_ARCHS[@]}"; do
  MINOS="$(otool -arch "$arch" -l "$EXECUTABLE" | awk '$1 == "minos" && !found { print $2; found = 1 }')"
  if [[ -z "$MINOS" ]]; then
    echo "LC_BUILD_VERSION minos is missing for $arch."
    exit 1
  fi
  if [[ "$MINOS" != "$EXPECTED_TARGET" ]]; then
    echo "$arch Mach-O minos is $MINOS; expected $EXPECTED_TARGET."
    exit 1
  fi
done

echo "Verified $APP_DIR (macOS >= $EXPECTED_TARGET; architectures: ${ACTUAL_ARCHS[*]}; signed identifier: $PLIST_IDENTIFIER)"
