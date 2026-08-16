#!/usr/bin/env bash
# Serve the dashboard on localhost. Never open index.html over file:// —
# the page uses ES modules, which the file: protocol blocks.
#
#   ./serve.sh            → http://localhost:5173
#   ./serve.sh 8080       → http://localhost:8080
set -euo pipefail

PORT="${1:-5173}"
cd "$(dirname "$0")"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already serving. Reusing it: http://localhost:$PORT"
  exit 0
fi

echo "Serving on http://localhost:$PORT"
exec python3 -m http.server "$PORT" --bind 127.0.0.1
