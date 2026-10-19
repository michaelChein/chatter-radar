#!/bin/bash
# Chatter Radar installer for macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/michaelChein/chatter-radar/main/install.sh | bash
#
# What it does (no sudo, nothing outside your home folder):
#   1. downloads Chatter Radar into ~/.radar
#   2. installs its own private Python (via uv), so your system Python is never touched
#   3. starts Chatter Radar in the background at login (http://127.0.0.1:8080, reachable only from this Mac)
#   4. starts a small collector that reads WhatsApp for Mac's local database every 2 minutes
#   5. opens your browser on a short setup page
#
# Running it again updates Chatter Radar and keeps your data. Remove everything with: ~/.radar/bin/radar uninstall
#
# Environment overrides (rarely needed):
#   RADAR_HOME=~/.radar       install location
#   RADAR_REPO=michaelChein/chatter-radar    GitHub repo to download from
#   RADAR_REF=main            branch or tag
#   RADAR_SRC=/path/to/radar  install from a local checkout instead of downloading
#   RADAR_LABEL=com.radar     launchd label prefix
#   RADAR_PORT=8080           preferred port (the next free one is used if taken)
#   RADAR_NO_OPEN=1           don't open the browser / System Settings

# Everything lives inside main() and is called on the last line, so a download that is cut off halfway
# can never run a partial script.
main() {
  set -Eeuo pipefail

  RADAR_REPO="${RADAR_REPO:-michaelChein/chatter-radar}"
  RADAR_REF="${RADAR_REF:-main}"
  H="${RADAR_HOME:-$HOME/.radar}"
  LABEL="${RADAR_LABEL:-com.radar}"
  PY_VERSION="3.12"
  UID_NUM="$(id -u)"
  LOG="$H/logs/install.log"

  say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
  ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
  warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
  die()  { printf '\n\033[31mInstall failed:\033[0m %s\n' "$*" >&2
           printf 'Details: %s\n' "$LOG" >&2
           printf 'If you need help, open an issue and include the last lines of that file.\n' >&2
           exit 1; }

  # ---------------------------------------------------------------- preflight
  [ "$(uname -s)" = "Darwin" ] || { echo "Chatter Radar's one-command install is for macOS only." >&2; exit 1; }
  [ "$(id -u)" -ne 0 ] || { echo "Please run this as your normal user, not with sudo." >&2; exit 1; }
  command -v curl >/dev/null || { echo "curl is required." >&2; exit 1; }
  case "$(uname -m)" in
    arm64)  UV_ARCH="aarch64-apple-darwin" ;;
    x86_64) UV_ARCH="x86_64-apple-darwin" ;;
    *) echo "Unsupported CPU: $(uname -m)" >&2; exit 1 ;;
  esac

  mkdir -p "$H"/{bin,logs,data,collector}
  chmod 700 "$H"
  : > "$LOG"
  exec > >(tee -a "$LOG") 2>&1
  trap 'die "step \"${STEP:-?}\" failed (line $LINENO)"' ERR

  UPDATING=0; [ -f "$H/config.env" ] && UPDATING=1
  say "$([ $UPDATING = 1 ] && echo 'Updating Chatter Radar' || echo 'Installing Chatter Radar')"

  # ---------------------------------------------------------------- 1. code
  STEP="download Chatter Radar"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  if [ -n "${RADAR_SRC:-}" ]; then
    mkdir -p "$TMP/src"
    rsync -a --exclude .git --exclude .venv --exclude data --exclude '.env' --exclude __pycache__ \
          --exclude .pytest_cache --exclude docs "$RADAR_SRC"/ "$TMP/src"/
  else
    curl -fsSL "https://github.com/$RADAR_REPO/archive/$RADAR_REF.tar.gz" -o "$TMP/radar.tgz" \
      || die "couldn't download https://github.com/$RADAR_REPO (is the repo public, and are you online?)"
    mkdir -p "$TMP/src"
    tar -xzf "$TMP/radar.tgz" -C "$TMP/src" --strip-components=1
  fi
  [ -f "$TMP/src/app/main.py" ] || die "the download doesn't look like Chatter Radar"
  rm -rf "$H/app.new"; mkdir "$H/app.new"
  rsync -a "$TMP/src/app" "$TMP/src/collectors" "$TMP/src/requirements.txt" "$H/app.new/"
  ok "downloaded"

  # ---------------------------------------------------------------- 2. python
  STEP="install Python"
  export UV_PYTHON_INSTALL_DIR="$H/python" UV_PYTHON_PREFERENCE=only-managed UV_NO_PROGRESS=1
  if [ ! -x "$H/bin/uv" ]; then
    curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$UV_ARCH.tar.gz" -o "$TMP/uv.tgz" \
      || die "couldn't download the uv Python manager (are you online?)"
    tar -xzf "$TMP/uv.tgz" -C "$TMP"
    install -m 755 "$TMP/uv-$UV_ARCH/uv" "$H/bin/uv"
  fi
  "$H/bin/uv" python install "$PY_VERSION" >/dev/null
  [ -x "$H/venv/bin/python" ] || "$H/bin/uv" venv --python "$PY_VERSION" "$H/venv" >/dev/null
  STEP="install dependencies"
  "$H/bin/uv" pip install --quiet --python "$H/venv/bin/python" -r "$H/app.new/requirements.txt"
  ok "Python $PY_VERSION and dependencies ready"

  # stop the running copy before swapping the code in
  STEP="swap in the new version"
  stop_agents
  rm -rf "$H/app.old"; [ -d "$H/app" ] && mv "$H/app" "$H/app.old"
  mv "$H/app.new" "$H/app"; rm -rf "$H/app.old"

  # ---------------------------------------------------------------- 3. config (kept across updates)
  STEP="write configuration"
  if [ ! -f "$H/config.env" ]; then
    TOKEN="$("$H/venv/bin/python" -c 'import secrets; print(secrets.token_hex(24))')"
    PORT="$("$H/venv/bin/python" - "${RADAR_PORT:-8080}" <<'PYEOF'
import socket, sys
p = int(sys.argv[1])
while True:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); break
    except OSError:
        p += 1
    finally:
        s.close()
print(p)
PYEOF
)"
    umask 077
    cat > "$H/config.env" <<EOF
RADAR_LOCAL=1
RADAR_PORT=$PORT
RADAR_INGEST_TOKEN=$TOKEN
RADAR_DB=$H/data/radar.db
RADAR_FILES=$H/data/files
RADAR_COLLECTOR_LABEL=$LABEL.collector
EOF
    umask 022
  fi
  # added after the first release: keep older installs working on update
  grep -q '^RADAR_COLLECTOR_LABEL=' "$H/config.env" || echo "RADAR_COLLECTOR_LABEL=$LABEL.collector" >> "$H/config.env"
  set -a; . "$H/config.env"; set +a
  printf 'RADAR_REPO=%s\nRADAR_REF=%s\n' "$RADAR_REPO" "$RADAR_REF" > "$H/install.env"

  ME_NAME="$(id -F 2>/dev/null | awk '{print $1}')"
  "$H/venv/bin/python" - "$H" "$RADAR_PORT" "$RADAR_INGEST_TOKEN" "${ME_NAME:-Me}" <<'PYEOF'
import json, os, sys
home, port, token, me = sys.argv[1:5]
path = os.path.join(home, "collector", "config.json")
cfg = {}
if os.path.exists(path):
    cfg = json.load(open(path))
cfg.update({"RADAR_URL": f"http://127.0.0.1:{port}", "RADAR_TOKEN": token, "COLLECTOR_NAME": "mac"})
cfg.setdefault("ME_NAME", me)
cfg.setdefault("BACKFILL_DAYS", 14)
json.dump(cfg, open(path, "w"), indent=1)
os.chmod(path, 0o600)
PYEOF

  # ---------------------------------------------------------------- 4. helper commands
  STEP="write helper commands"
  cat > "$H/bin/radar-server" <<EOF
#!/bin/bash
# Started by launchd. Loads config.env, then serves Chatter Radar on localhost only.
set -a; . "$H/config.env"; set +a
cd "$H/app"
exec "$H/venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "\$RADAR_PORT" --log-level warning
EOF
  chmod 755 "$H/bin/radar-server"
  write_cli

  # ---------------------------------------------------------------- 5. background services
  STEP="start background services"
  mkdir -p "$HOME/Library/LaunchAgents"
  write_plist "$LABEL.server" "$H/bin/radar-server" "" "<key>KeepAlive</key><true/>" "$H/logs/server.log"
  write_plist "$LABEL.collector" "$H/venv/bin/python" "$H/app/collectors/mac_whatsapp.py" \
              "<key>StartInterval</key><integer>120</integer>" "$H/logs/collector.log" \
              "<key>EnvironmentVariables</key><dict><key>RADAR_COLLECTOR_DIR</key><string>$H/collector</string></dict>"
  for svc in server collector; do start_agent "$LABEL.$svc"; done

  STEP="wait for Chatter Radar to answer"
  URL="http://127.0.0.1:$RADAR_PORT"
  for _ in $(seq 1 40); do
    curl -fsS "$URL/api/health" >/dev/null 2>&1 && break
    sleep 0.75
  done
  curl -fsS "$URL/api/health" >/dev/null 2>&1 || { tail -n 20 "$H/logs/server.log" 2>/dev/null || true; die "Chatter Radar didn't start"; }
  ok "Chatter Radar is running at $URL"
  # the collector's first run (at load) raced the server's startup: run it again now that the server is up
  launchctl kickstart -k "gui/$UID_NUM/$LABEL.collector" >/dev/null 2>&1 || true

  # ---------------------------------------------------------------- 6. hand over to the setup page
  say "Chatter Radar is ready"
  echo "  A setup page is opening in your browser: paste your Claude API key and choose the chats to watch."
  echo "  If macOS blocks access to WhatsApp's data on your Mac, that page walks you through allowing it."
  if [ ! -d "$HOME/Library/Group Containers/group.net.whatsapp.WhatsApp.shared" ]; then
    warn "WhatsApp for Mac doesn't seem to be installed. Install it from the Mac App Store and link it to your phone."
  fi
  echo
  echo "Manage Chatter Radar with: $H/bin/radar  (open | status | logs | restart | update | reset-setup | uninstall)"

  [ "${RADAR_NO_OPEN:-}" = "1" ] || open "$URL" || true
}

# ------------------------------------------------------------------ helpers
stop_agents() {
  local svc label
  for svc in server collector; do
    label="$LABEL.$svc"
    launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null \
      || launchctl unload "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
    # bootout returns before launchd has finished tearing the job down; starting it again too early fails
    for _ in $(seq 1 40); do
      launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1 || break
      sleep 0.25
    done
  done
}

start_agent() {
  local plist="$HOME/Library/LaunchAgents/$1.plist" attempt
  for attempt in 1 2 3 4 5; do
    launchctl bootstrap "gui/$UID_NUM" "$plist" 2>/dev/null && return 0
    launchctl print "gui/$UID_NUM/$1" >/dev/null 2>&1 && return 0   # already loaded
    sleep 1
  done
  launchctl load -w "$plist"
}

# write_plist <label> <program> <script-or-empty> <extra-keys> <logfile> [more-keys]
write_plist() {
  local label="$1" prog="$2" script="$3" keys="$4" log="$5" more="${6:-}"
  local args="<string>$prog</string>"
  [ -n "$script" ] && args="$args<string>$script</string>"
  cat > "$HOME/Library/LaunchAgents/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>RunAtLoad</key><true/>
  $keys
  $more
  <key>StandardOutPath</key><string>$log</string>
  <key>StandardErrorPath</key><string>$log</string>
</dict></plist>
EOF
}

write_cli() {
  cat > "$H/bin/radar" <<EOF
#!/bin/bash
# Chatter Radar control script.
H="$H"; LABEL="$LABEL"; UID_NUM="\$(id -u)"
set -a; . "\$H/config.env"; set +a
URL="http://127.0.0.1:\$RADAR_PORT"
agents() { for s in server collector; do echo "\$LABEL.\$s"; done; }
case "\${1:-status}" in
  open)    open "\$URL" ;;
  status)
    if curl -fsS "\$URL/api/health" >/dev/null 2>&1; then echo "Chatter Radar is running: \$URL"; else echo "Chatter Radar is NOT answering at \$URL (try: radar restart, radar logs)"; fi
    tail -n 3 "\$H/logs/collector.log" 2>/dev/null | sed 's/^/  collector: /' ;;
  logs)    tail -n 40 "\$H/logs/server.log" "\$H/logs/collector.log" ;;
  reset-setup)
    # start the first-run checklist over (for testing): forget the API key, the "setup done" flag and the collector status
    "\$H/venv/bin/python" - "\$RADAR_DB" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("DELETE FROM kv WHERE key IN ('anthropic_api_key', 'setup_done') OR key LIKE 'collector_status:%'")
c.commit()
PYEOF
    echo "Setup reset. Reload the page." ;;
  restart) for a in \$(agents); do launchctl kickstart -k "gui/\$UID_NUM/\$a"; done; echo restarted ;;
  update)
    . "\$H/install.env"
    exec bash -c "\\\$(curl -fsSL https://raw.githubusercontent.com/\$RADAR_REPO/\$RADAR_REF/install.sh)" ;;
  uninstall)
    for a in \$(agents); do
      launchctl bootout "gui/\$UID_NUM/\$a" 2>/dev/null || true
      rm -f "\$HOME/Library/LaunchAgents/\$a.plist"
    done
    if [ "\${2:-}" = "--purge" ]; then
      rm -rf "\$H"; echo "Chatter Radar and all its data were removed."
    else
      rm -rf "\$H/app" "\$H/venv" "\$H/python" "\$H/bin" "\$H/install.env"
      echo "Chatter Radar was removed. Your data is kept in \$H/data. Delete it with: rm -rf \$H"
    fi
    echo "Also remove the Full Disk Access entry in System Settings if you added one." ;;
  *) echo "usage: radar [open|status|logs|restart|update|reset-setup|uninstall [--purge]]"; exit 1 ;;
esac
EOF
  chmod 755 "$H/bin/radar"
}

main "$@"
