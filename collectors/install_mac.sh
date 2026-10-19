#!/bin/bash
# Install the Radar WhatsApp collector on a Mac as a launchd agent (runs every 2 minutes).
# Usage: ./install_mac.sh https://<radar-host> <ingest-token> [collector-name] [your-name]
set -euo pipefail
URL=${1:?Radar URL}; TOKEN=${2:?ingest token}; NAME=${3:-mac}; ME=${4:-Me}
DIR="$HOME/.radar-collector"
PY=/usr/bin/python3
mkdir -p "$DIR"
cp "$(dirname "$0")/mac_whatsapp.py" "$DIR/"
cat > "$DIR/config.json" <<EOF
{"RADAR_URL": "$URL", "RADAR_TOKEN": "$TOKEN", "COLLECTOR_NAME": "$NAME", "ME_NAME": "$ME", "BACKFILL_DAYS": 14}
EOF
chmod 600 "$DIR/config.json"

PLIST="$HOME/Library/LaunchAgents/com.radar.whatsapp-collector.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.radar.whatsapp-collector</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/mac_whatsapp.py</string></array>
  <key>StartInterval</key><integer>120</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/collector.log</string>
  <key>StandardErrorPath</key><string>$DIR/collector.log</string>
</dict></plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Installed. One manual step:"
echo "  System Settings → Privacy & Security → Full Disk Access → add $PY"
echo "  (press Cmd+Shift+G in the file picker and type /usr/bin/python3)"
echo "Then check: tail -f $DIR/collector.log"
