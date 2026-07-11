#!/bin/zsh
set -euo pipefail

ROOT="${COCHL_SENSE_CLOUD_LIVE_DEMO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Cochl.Sense Cloud Live Demo error: .venv was not found. Run backend setup from README first."
  exit 1
fi

if [[ ! -f "$ROOT/frontend/dist/index.html" ]]; then
  echo "Cochl.Sense Cloud Live Demo error: frontend/dist was not found. Run scripts/build-macos-app.sh first."
  exit 1
fi

cd "$ROOT"

# exec preserves NSTask's PID. The Python runner makes that PID a process-group
# leader, holds the ephemeral listen socket continuously, and gives the same
# socket to Uvicorn. The native wrapper can therefore terminate and reap the
# complete server group without a find-free-port/bind race.
exec "$PYTHON" "$ROOT/scripts/run_macos_server.py"
