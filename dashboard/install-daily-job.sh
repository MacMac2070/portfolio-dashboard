#!/bin/bash
# Install (or reinstall) the daily snapshot LaunchAgent.
#
#   ./install-daily-job.sh          install / reinstall
#   ./install-daily-job.sh --remove uninstall
#
# launchd rather than cron: it survives reboot and logout, needs no terminal
# open, and on modern macOS cron additionally needs Full Disk Access granted to
# /usr/sbin/cron. Nothing here involves Claude.

set -euo pipefail

LABEL="com.portfolio-dashboard.refresh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/opt/anaconda3/bin/python3"

# 23:30 local — after the US close, so the recorded NAV is a complete day.
HOUR=23
MINUTE=30

if [[ "${1:-}" == "--remove" ]]; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON not found. That interpreter has ib_async and openbb;" >&2
  echo "       the system python3 does not. Edit PYTHON in this script if it moved." >&2
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
    <string>$DIR/adapter/refresh.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$DIR</string>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MINUTE</integer>
  </dict>

  <!-- If the Mac was asleep at the scheduled time, run once on wake. -->
  <key>RunAtLoad</key>
  <false/>

  <key>StandardOutPath</key>
  <string>$DIR/logs/refresh.log</string>
  <key>StandardErrorPath</key>
  <string>$DIR/logs/refresh.log</string>

  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

echo "Installed $LABEL"
echo "  runs      : daily at $(printf '%02d:%02d' $HOUR $MINUTE) local"
echo "  script    : $DIR/adapter/refresh.py"
echo "  log       : $DIR/logs/refresh.log"
echo
echo "Run it now to check:   launchctl kickstart -p gui/$UID/$LABEL"
echo "Status:                launchctl print gui/$UID/$LABEL | head -20"
echo "Remove:                ./install-daily-job.sh --remove"
echo
echo "Note: IB Gateway must be running and logged in when the job fires."
echo "If it isn't, the job logs and exits; re-run adapter/backfill.py to"
echo "recover the missed day from Flex."
