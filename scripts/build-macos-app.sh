#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT/CochlSenseCloudLiveDemo.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
# Clang module cache entries embed their absolute cache path. Keeping the cache
# inside a movable project directory can make a copied/renamed checkout fail
# with a PCH path mismatch, so use a stable per-user temporary location.
MODULE_CACHE_DIR="${TMPDIR:-/tmp}/cochl-sense-cloud-live-demo-clang-module-cache"

if ! command -v clang >/dev/null 2>&1; then
  echo "clang was not found. Install Xcode Command Line Tools first."
  exit 1
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo ".venv was not found. Run backend setup from README first."
  exit 1
fi

cd "$ROOT/frontend"
PATH="$HOME/.nvm/versions/node/v24.14.0/bin:$PATH" npm run build

mkdir -p "$MACOS_DIR"
mkdir -p "$MODULE_CACHE_DIR"
cp "$ROOT/macos/Info.plist" "$CONTENTS_DIR/Info.plist"

clang \
  -fobjc-arc \
  -fmodules \
  -fmodules-cache-path="$MODULE_CACHE_DIR" \
  "$ROOT/macos/CochlSenseCloudLiveDemoApp.m" \
  -o "$MACOS_DIR/CochlSenseCloudLiveDemo" \
  -framework Cocoa \
  -framework WebKit

chmod +x "$MACOS_DIR/CochlSenseCloudLiveDemo"
chmod +x "$ROOT/scripts/run-macos-server.sh"
touch "$APP_DIR"

echo "Built $APP_DIR"
