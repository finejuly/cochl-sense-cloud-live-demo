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

PORT="$("$PYTHON" - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"

cd "$ROOT"

"$PYTHON" -m uvicorn backend.app.main:app --host 127.0.0.1 --port "$PORT" &
SERVER_PID=$!

stop_server() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}

trap stop_server INT TERM EXIT

for _ in {1..80}; do
  if "$PYTHON" - "$PORT" <<'PY' >/dev/null 2>&1
import json
import sys
from urllib.request import urlopen

port = sys.argv[1]
with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.25) as response:
    payload = json.loads(response.read().decode("utf-8"))
    raise SystemExit(0 if payload.get("status") == "ok" else 1)
PY
  then
    echo "Cochl.Sense Cloud Live Demo is running at http://127.0.0.1:$PORT"
    wait "$SERVER_PID"
    exit $?
  fi
  sleep 0.1
done

echo "Cochl.Sense Cloud Live Demo error: server did not become ready."
exit 1
