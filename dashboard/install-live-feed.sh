#!/bin/bash
# Optional: keep the live dashboard server running via launchd.
#
#   ./install-live-feed.sh          install / reinstall
#   ./install-live-feed.sh --remove uninstall
#
# Entirely separate from install-daily-job.sh, which schedules the once-a-day
# NAV snapshot and Flex import. This one keeps serve.py — and with it the held-
# open IB Gateway connection — alive continuously and restarts it on failure.
#
# You do not need this to use the dashboard. Running
#   /opt/anaconda3/bin/python3 serve.py 5174
# in a terminal does the same thing while you watch the logs.

set -euo pipefail

LABEL="com.portfolio-dashboard.live"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/opt/anaconda3/bin/python3"
PORT="${PORT:-5174}"

if [[ "${1:-}" == "--remove" ]]; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON not found. That interpreter has ib_async; the system" >&2
  echo "       python3 does not. Edit PYTHON in this script if it moved." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$DIR/serve.py</string>
    <string>$PORT</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$DIR</string>

  <key>RunAtLoad</key>
  <true/>

  <!-- The feed reconnects to Gateway on its own, so a crash is the only
       reason to restart the process; throttle so a boot loop stays quiet. -->
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>

  <key>StandardOutPath</key>
  <string>$DIR/logs/live.log</string>
  <key>StandardErrorPath</key>
  <string>$DIR/logs/live.log</string>

  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

echo "Installed $LABEL"
echo "  serves    : http://localhost:$PORT"
echo "  endpoint  : http://localhost:$PORT/api/snapshot"
echo "  log       : $DIR/logs/live.log"
echo
echo "Status:  launchctl print gui/$UID/$LABEL | head -20"
echo "Remove:  ./install-live-feed.sh --remove"
echo
echo "It holds an IB Gateway connection open for as long as it runs (clientId 11)."
echo "If Gateway is closed the feed retries with backoff and the page shows stale."
