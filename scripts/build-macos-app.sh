#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT/CochlSenseCloudLiveDemo.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
REQUESTED_ARCHS="${COCHL_MACOS_ARCHS:-$(uname -m)}"
CODESIGN_IDENTITY="${COCHL_CODESIGN_IDENTITY:--}"
CLEAN_BUILD=0
RUN_NPM_CI=0
# Clang module cache entries embed their absolute cache path. Keeping the cache
# inside a movable project directory can make a copied/renamed checkout fail
# with a PCH path mismatch, so use a stable per-user temporary location.
MODULE_CACHE_DIR="${TMPDIR:-/tmp}/cochl-sense-cloud-live-demo-clang-module-cache"

usage() {
  cat <<'EOF'
Usage: scripts/build-macos-app.sh [--clean] [--npm-ci]

  --clean   Remove prior app/frontend build output and run npm ci first.
  --npm-ci  Reinstall frontend dependencies exactly from package-lock.json.

Environment:
  COCHL_MACOS_ARCHS="arm64 x86_64"  Build a universal2 wrapper when the
                                      installed SDK supports both targets.
  COCHL_CODESIGN_IDENTITY="..."       Sign with a stable local identity.
                                      Defaults to an ad-hoc development signature.
  MACOSX_DEPLOYMENT_TARGET=13.0       Explicit target; it must match the
                                      checked-in plist (currently 13.0).
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --clean)
      CLEAN_BUILD=1
      RUN_NPM_CI=1
      ;;
    --npm-ci)
      RUN_NPM_CI=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

for command_name in clang codesign plutil xattr; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name was not found. Install Xcode Command Line Tools first."
    exit 1
  fi
done

PLIST_DEPLOYMENT_TARGET="$(
  plutil -extract LSMinimumSystemVersion raw "$ROOT/macos/Info.plist"
)"
BUNDLE_IDENTIFIER="$(
  plutil -extract CFBundleIdentifier raw "$ROOT/macos/Info.plist"
)"
if [[ "$DEPLOYMENT_TARGET" != "$PLIST_DEPLOYMENT_TARGET" ]]; then
  echo "MACOSX_DEPLOYMENT_TARGET ($DEPLOYMENT_TARGET) must match Info.plist ($PLIST_DEPLOYMENT_TARGET)."
  exit 1
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo ".venv was not found. Run backend setup from README first."
  exit 1
fi

if [[ ! -f "$ROOT/.nvmrc" ]]; then
  echo ".nvmrc was not found. The macOS build requires a checked-in Node version."
  exit 1
fi

NODE_VERSION="$(tr -d '[:space:]' < "$ROOT/.nvmrc")"
if [[ ! "$NODE_VERSION" =~ '^[0-9]+\.[0-9]+\.[0-9]+$' ]]; then
  echo "Invalid .nvmrc version: $NODE_VERSION"
  exit 1
fi

NVM_NODE_BIN="${NVM_DIR:-$HOME/.nvm}/versions/node/v${NODE_VERSION}/bin"
if [[ -x "$NVM_NODE_BIN/node" ]]; then
  PATH="$NVM_NODE_BIN:$PATH"
fi
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "Node.js $NODE_VERSION and npm were not found. Run 'nvm install' first."
  exit 1
fi
if [[ "$(node --version)" != "v$NODE_VERSION" ]]; then
  echo "Node.js $(node --version) is active, but .nvmrc requires v$NODE_VERSION. Run 'nvm install && nvm use'."
  exit 1
fi

typeset -a CLANG_ARCH_ARGS
typeset -a NORMALIZED_ARCHS
for arch in ${(z)REQUESTED_ARCHS}; do
  case "$arch" in
    arm64|x86_64)
      if (( ${NORMALIZED_ARCHS[(Ie)$arch]} == 0 )); then
        NORMALIZED_ARCHS+=("$arch")
        CLANG_ARCH_ARGS+=("-arch" "$arch")
      fi
      ;;
    *)
      echo "Unsupported macOS architecture '$arch'; use arm64, x86_64, or both."
      exit 1
      ;;
  esac
done
if (( ${#NORMALIZED_ARCHS[@]} == 0 )); then
  echo "COCHL_MACOS_ARCHS did not contain a supported architecture."
  exit 1
fi

if (( CLEAN_BUILD )); then
  rm -rf "$APP_DIR" "$ROOT/frontend/dist"
fi

cd "$ROOT/frontend"
if (( RUN_NPM_CI )) || [[ ! -d node_modules ]]; then
  if [[ ! -f package-lock.json ]]; then
    echo "frontend/package-lock.json was not found; npm ci cannot reproduce dependencies."
    exit 1
  fi
  npm ci
fi
npm run build

mkdir -p "$MACOS_DIR"
mkdir -p "$MODULE_CACHE_DIR"
cp "$ROOT/macos/Info.plist" "$CONTENTS_DIR/Info.plist"

if ! clang \
    -fobjc-arc \
    -fmodules \
    -fmodules-cache-path="$MODULE_CACHE_DIR" \
    -mmacosx-version-min="$DEPLOYMENT_TARGET" \
    "${CLANG_ARCH_ARGS[@]}" \
    "$ROOT/macos/CochlSenseCloudLiveDemoApp.m" \
    -o "$MACOS_DIR/CochlSenseCloudLiveDemo" \
    -framework Cocoa \
    -framework WebKit; then
  echo "The installed macOS SDK could not build requested architectures: ${NORMALIZED_ARCHS[*]}" >&2
  echo "Build the current architecture by unsetting COCHL_MACOS_ARCHS, or install an SDK with both slices." >&2
  exit 1
fi

chmod +x "$MACOS_DIR/CochlSenseCloudLiveDemo"
chmod +x "$ROOT/scripts/run-macos-server.sh"
touch "$APP_DIR"

# Clang's linker signature covers only an individual Mach-O slice. In
# particular, a universal2 wrapper otherwise leaves the .app without one valid
# bundle identity, which makes macOS repeat protected-folder permission prompts
# for descendant shell/Python processes. Seal the whole bundle after every
# build so Info.plist and all native slices share one identity.
xattr -cr "$APP_DIR"
codesign \
  --force \
  --sign "$CODESIGN_IDENTITY" \
  --identifier "$BUNDLE_IDENTIFIER" \
  "$APP_DIR"
codesign --verify --deep "$APP_DIR"

SIGNING_LABEL="ad-hoc"
if [[ "$CODESIGN_IDENTITY" != "-" ]]; then
  SIGNING_LABEL="$CODESIGN_IDENTITY"
fi
echo "Built $APP_DIR (macOS >= $DEPLOYMENT_TARGET; architectures: ${NORMALIZED_ARCHS[*]}; signing: $SIGNING_LABEL)"
if (( ${#NORMALIZED_ARCHS[@]} > 1 )); then
  echo "The wrapper executable is universal2; the external repo .venv must still run on this Mac."
fi
