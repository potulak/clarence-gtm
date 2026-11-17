#!/bin/bash
# Set this Mac up as the always-on machine for the Reddit loop.
#
#   bash reddit/collector/setup-machine.sh
#
# (`scratch/reddit/setup-machine.sh` still works: it is a symlink to this file.)
#
# It installs two launchd jobs: the collector every hour, and the cockpit as a
# service that comes back after a reboot. It never copies a secret. If the API
# key is missing it tells you where to put it and stops.
set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$(command -v python3 || echo /usr/bin/python3)"
# Overridable so this script can be tested without touching a real machine.
AGENTS="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LAUNCHCTL="${LAUNCHCTL_BIN:-launchctl}"
KEYFILE="$HOME/Library/Application Support/clarence-aeo/.env"
PORT="${COCKPIT_PORT:-4360}"

case "$REPO" in
  "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Downloads"/*)
    echo "This checkout is at $REPO."
    echo
    echo "macOS will not let a background agent read Documents, Desktop or Downloads."
    echo "Everything would install cleanly and then fail with \"Operation not"
    echo "permitted\" on the hour, every hour."
    echo
    echo "Move it somewhere plain first, then run this again:"
    echo "    mv \"$REPO\" ~/clarence && cd ~/clarence"
    exit 1;;
esac

echo "repo:   $REPO"
echo "python: $PY ($("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'))"

"$PY" - <<'PYCHECK'
import sys
if sys.version_info < (3, 9):
    sys.exit("python 3.9 or newer is needed. Install it, then run this again.")
PYCHECK

if [ ! -f "$KEYFILE" ] || ! grep -q ANTHROPIC_API_KEY "$KEYFILE" 2>/dev/null; then
  echo
  echo "No API key found. Put yours here, then run this again:"
  echo "    $KEYFILE"
  echo "    ANTHROPIC_API_KEY=sk-ant-..."
  echo
  echo "Copy it across yourself. Do not let anything paste a key for you."
  exit 1
fi
echo "key:    found"

mkdir -p "$AGENTS" "$REPO/scratch/reddit/logs"

write_agent () {   # label, program-args-xml, extra-xml
  cat > "$AGENTS/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key>
  <array>$2</array>
  $3
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StandardOutPath</key><string>$REPO/scratch/reddit/logs/$1.out</string>
  <key>StandardErrorPath</key><string>$REPO/scratch/reddit/logs/$1.err</string>
</dict>
</plist>
PLIST
  "$LAUNCHCTL" unload "$AGENTS/$1.plist" 2>/dev/null || true
  "$LAUNCHCTL" load "$AGENTS/$1.plist"
  echo "loaded: $1"
}

# These two point at the repo copies rather than at the scratch/ symlinks that
# lead to the same files. A launchd job whose program is a link pointing at
# nothing fails on the hour, every hour, into a log nobody reads, and a
# collector that is not running looks exactly like a quiet week.
write_agent "app.clarence.reddit.hourly" \
  "<string>$REPO/reddit/collector/hourly.sh</string>" \
  "<key>StartInterval</key><integer>3600</integer><key>RunAtLoad</key><false/>"

# The board itself, on the network so you can read it from your other Mac.
write_agent "app.clarence.cockpit" \
  "<string>$PY</string><string>$REPO/reddit/cockpit/server.py</string>" \
  "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>EnvironmentVariables</key><dict>
    <key>COCKPIT_HOST</key><string>0.0.0.0</string>
    <key>COCKPIT_PORT</key><string>$PORT</string>
  </dict>"

LAN="$("$PY" -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()" 2>/dev/null || echo localhost)"
echo
echo "Done. From your other Mac, open:   http://$LAN:$PORT"
echo "The collector runs every hour. Logs: $REPO/scratch/reddit/logs/"
echo
echo "Two things worth knowing:"
echo "  · Nothing on that board asks for a password, so keep it to a network you trust."
echo "  · Set this Mac to never sleep, or it only collects while it is awake:"
echo "      System Settings > Lock Screen, and Energy Saver > Prevent automatic sleeping"
echo
echo "To stop it all:"
echo "  launchctl unload $AGENTS/app.clarence.reddit.hourly.plist"
echo "  launchctl unload $AGENTS/app.clarence.cockpit.plist"
