#!/bin/bash
# Stop the always-on Mac from running itself flat.
#
#   bash  reddit/collector/battery-guard.sh status
#   bash  reddit/collector/battery-guard.sh selftest     # the rules, no machine
#   sudo bash reddit/collector/battery-guard.sh install   # watch every 5 minutes
#   sudo bash reddit/collector/battery-guard.sh uninstall
#   sudo bash reddit/collector/battery-guard.sh check     # one pass, by hand
#
# The problem it solves. `awake.sh on` sets SleepDisabled so a closed lid stops
# mattering, which is the only way to collect with the lid shut. That switch is
# global: macOS offers no "never sleep on the charger but do on battery". So an
# unplugged Mac with it on does not nap, it runs flat and stops dead, and a
# collector that is off looks exactly like a quiet week. On 2026-10-18 the Air
# was in that state with 16 hours of battery and nothing watching it.
#
# What it does. Below the floor on battery, it re-enables sleep so the machine
# naps instead of dying. Back on the charger, it puts the switch back.
#
# The rule that makes it safe to leave running: it only ever undoes ITS OWN
# change. Lowering the switch writes a hold file, and only a hold file lets it
# raise the switch again. So if you have deliberately left sleep enabled, this
# never turns it off behind you, and it can never decide the policy for you.
# It is a seatbelt, not a driver.
#
# Nothing is lost when the machine naps. A collector run reads 24 hours of new
# posts and a week of search, so a gap shorter than a day catches itself up.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# Beside the store, not beside the code, for the same reason everything else
# here is: the code is version controlled and the machine's own state is not.
STATE="$REPO/scratch/reddit/battery-guard.hold"
LOG="$REPO/scratch/reddit/logs/battery-guard.log"
LABEL="app.clarence.battery.guard"
# Overridable for the same reason setup-machine.sh makes its two overridable: so
# the install path can be exercised without root and without touching the real
# launchd. Nothing in normal use sets them.
PLIST="${CLARENCE_GUARD_PLIST:-/Library/LaunchDaemons/$LABEL.plist}"
LAUNCHCTL="${LAUNCHCTL_BIN:-launchctl}"
FLOOR="${CLARENCE_BATTERY_FLOOR:-20}"
EVERY=300

say() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" >> "$LOG"; }

power_now() { pmset -g ps | head -1 | grep -q "AC Power" && echo AC || echo battery; }
sleep_off()  { pmset -g | awk '/SleepDisabled/{print $2}'; }
holding()    { [ -f "$STATE" ] && echo yes || echo no; }

pct_now() {
  # A Mac with no battery prints no percentage. 100 is the safe reading: it
  # cannot die, so there is nothing for this to do.
  local p
  p="$(pmset -g batt | grep -o '[0-9]\{1,3\}%' | head -1 | tr -d '%' || true)"
  case "$p" in ''|*[!0-9]*) echo 100 ;; *) echo "$p" ;; esac
}

# decide <AC|battery> <percent> <sleep-disabled 0|1> <holding yes|no>
#
# Pure: no machine, no files. That is what selftest exercises, because the one
# state worth being sure about is the one you cannot stage on a real laptop
# without flattening it first.
decide() {
  local power="$1" pct="$2" off="$3" hold="$4"
  if [ "$power" = "battery" ] && [ "$pct" -le "$FLOOR" ] && [ "$off" = "1" ]; then
    echo let-it-sleep
  elif [ "$power" = "AC" ] && [ "$hold" = "yes" ] && [ "$off" = "0" ]; then
    echo restore-awake
  elif [ "$power" = "AC" ] && [ "$hold" = "yes" ] && [ "$off" = "1" ]; then
    # Someone raised it by hand while we were holding. Nothing to restore, so
    # let go rather than keep a claim on a switch we did not set.
    echo drop-the-hold
  else
    echo nothing
  fi
}

do_action() {
  case "$1" in
    let-it-sleep)
      pmset -a disablesleep 0
      date '+%Y-%m-%d %H:%M:%S' > "$STATE"
      say "battery at ${2}%, at or under the ${FLOOR}% floor: sleep re-enabled so it naps instead of dying. Will put it back on the charger."
      echo "battery ${2}%: sleep re-enabled, it will nap now. Plug it in to go back to 24/7."
      ;;
    restore-awake)
      pmset -a disablesleep 1
      rm -f "$STATE"
      say "back on the charger at ${2}%: sleep disabled again, lid can be closed."
      echo "on the charger: sleep disabled again, lid can be closed."
      ;;
    drop-the-hold)
      rm -f "$STATE"
      say "on the charger and the switch was already back on: hold dropped."
      echo "hold dropped, nothing to restore."
      ;;
    nothing) : ;;
  esac
}

case "${1:-status}" in

  check)
    [ "$(id -u)" = "0" ] || { echo "needs sudo: sudo bash $0 check"; exit 1; }
    mkdir -p "$(dirname "$LOG")"
    P="$(power_now)"; N="$(pct_now)"; O="$(sleep_off)"; H="$(holding)"
    A="$(decide "$P" "$N" "${O:-0}" "$H")"
    do_action "$A" "$N"
    [ "$A" = "nothing" ] && echo "nothing to do ($P, ${N}%, sleep-disabled=${O:-0}, holding=$H)"
    exit 0
    ;;

  install)
    [ "$(id -u)" = "0" ] || [ -n "$CLARENCE_GUARD_PLIST" ] \
      || { echo "needs sudo: sudo bash $0 install"; exit 1; }
    mkdir -p "$(dirname "$LOG")" "$(dirname "$PLIST")"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$HERE/battery-guard.sh</string>
    <string>check</string>
  </array>
  <key>StartInterval</key><integer>$EVERY</integer>
  <key>RunAtLoad</key><true/>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StandardOutPath</key><string>$REPO/scratch/reddit/logs/$LABEL.out</string>
  <key>StandardErrorPath</key><string>$REPO/scratch/reddit/logs/$LABEL.err</string>
</dict>
</plist>
PLIST
    if [ "$(id -u)" = "0" ]; then
      chown root:wheel "$PLIST"
      chmod 644 "$PLIST"
    fi
    # A daemon, not an agent, because only root may set this switch. It is also
    # why this cannot live inside hourly.sh, which runs as you.
    "$LAUNCHCTL" bootout "system/$LABEL" 2>/dev/null || true
    "$LAUNCHCTL" bootstrap system "$PLIST"
    echo "installed: $LABEL, every ${EVERY}s, floor ${FLOOR}%"
    echo "log: $LOG"
    ;;

  uninstall)
    [ "$(id -u)" = "0" ] || [ -n "$CLARENCE_GUARD_PLIST" ] \
      || { echo "needs sudo: sudo bash $0 uninstall"; exit 1; }
    "$LAUNCHCTL" bootout "system/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    # The hold outlives the daemon on purpose: if it is holding, sleep is
    # currently enabled BECAUSE of it, and you should be told rather than left
    # with a machine that quietly sleeps with its lid shut.
    if [ -f "$STATE" ]; then
      echo "removed. NOTE: it was still holding, so sleep is enabled right now."
      echo "For 24/7 again: sudo bash $HERE/awake.sh on   (and leave it plugged in)"
      rm -f "$STATE"
    else
      echo "removed."
    fi
    ;;

  status)
    P="$(power_now)"; N="$(pct_now)"; O="$(sleep_off)"; H="$(holding)"
    echo "daemon:          $("$LAUNCHCTL" print "system/$LABEL" >/dev/null 2>&1 && echo "loaded, every ${EVERY}s" || echo "NOT installed")"
    echo "floor:           ${FLOOR}%"
    echo "power source:    $P"
    echo "battery:         ${N}%"
    echo "sleep disabled:  ${O:-0}  ($([ "${O:-0}" = "1" ] && echo "stays awake, lid closed" || echo "sleeps normally"))"
    echo "holding:         $H$([ "$H" = "yes" ] && echo "  (it lowered the switch, and will raise it on the charger)")"
    echo
    echo "would do now:    $(decide "$P" "$N" "${O:-0}" "$H")"
    [ -f "$LOG" ] && { echo; echo "last 3 log lines:"; tail -3 "$LOG"; } || true
    ;;

  selftest)
    # power pct sleep-off holding  expected
    CASES="
battery 15 1 no   let-it-sleep
battery 20 1 no   let-it-sleep
battery 21 1 no   nothing
battery 15 0 yes  nothing
battery 15 0 no   nothing
battery 80 1 no   nothing
AC      100 0 yes restore-awake
AC      15  0 yes restore-awake
AC      100 0 no  nothing
AC      100 1 yes drop-the-hold
AC      100 1 no  nothing
battery 15 1 yes  let-it-sleep
"
    bad=0
    while read -r power pct off hold want; do
      [ -z "$power" ] && continue
      got="$(decide "$power" "$pct" "$off" "$hold")"
      [ "$got" = "$want" ] || bad=$((bad + 1))
      printf '  %s  %-7s %3s%% off=%s hold=%-3s -> %s\n' \
        "$([ "$got" = "$want" ] && echo 'ok  ' || echo 'FAIL')" \
        "$power" "$pct" "$off" "$hold" "$got"
    done <<< "$CASES"
    echo
    [ "$bad" = 0 ] && echo "all pass" || { echo "$bad FAILED"; exit 1; }
    ;;

  *) echo "usage: $0 [status|selftest|check|install|uninstall]"; exit 1 ;;
esac
