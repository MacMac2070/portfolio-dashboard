#!/bin/bash
# Install (or remove) the 12-hourly expense pull LaunchAgent.
#
#   ./install-pull-job.sh           install / reinstall
#   ./install-pull-job.sh --remove  uninstall
#
# DO NOT install until the aggregator client is implemented and the sandbox
# dry run (test_sandbox_pull.py) has shown a sane payload — build plan §10.
# Self-contained: modelled on the dashboard's install-daily-job.sh but shares
# nothing with it. Becomes a Vercel Cron entry at migration time.

set -euo pipefail

LABEL="com.expense-tracker.pull"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="/opt/anaconda3/bin/python3"

if [[ "${1:-}" == "--remove" ]]; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

mkdir -p "$DIR/logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PYTHON</string>
    <string>$DIR/api/cron_pull.py</string>
  </array>
  <key>StartCalendarInterval</key><array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>$DIR/logs/pull.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/pull.log</string>
</dict></plist>
PLIST

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
echo "Installed $LABEL — pulls at 08:00 and 20:00, logs to $DIR/logs/pull.log"
