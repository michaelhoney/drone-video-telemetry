#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=5002
URL="http://localhost:${PORT}"
LOG_FILE="/tmp/drone_video_telemetry_server_5002.log"

cd "$SCRIPT_DIR"

echo "Starting simple server in $SCRIPT_DIR on port $PORT..."
python3 -m http.server "$PORT" >"$LOG_FILE" 2>&1 &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID"
  fi
}

trap cleanup EXIT INT TERM

sleep 1

if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
  echo "The server failed to start. Details:"
  cat "$LOG_FILE"
  exit 1
fi

open "$URL"

echo "Browser opened at $URL"
echo "Press Control-C in this window to stop the server."

wait "$SERVER_PID"
