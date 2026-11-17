#!/bin/bash
# Keep this Mac awake, or let it sleep again.
#
#   bash reddit/collector/awake.sh status
#   sudo bash reddit/collector/awake.sh on     # run 24/7, lid closed
#   sudo bash reddit/collector/awake.sh off    # back to normal sleeping
#
# (`scratch/reddit/awake.sh` still works: it is a symlink to this file now, and
# it is the path the board prints when it sees this Mac running on battery.)
#
# "on" sets SleepDisabled, which is what makes a closed lid stop mattering. Note
# it is a GLOBAL switch, not one per power source: macOS gives you no way to say
# "never sleep on the charger but do on battery". So while it is on and unplugged,
# this Mac will run the battery flat and die rather than nap. Keep it plugged in.
set -e
ACTION="${1:-status}"

power() { pmset -g ps | head -1 | grep -q "AC Power" && echo AC || echo battery; }
state() { pmset -g | grep -i SleepDisabled | awk '{print $2}'; }

case "$ACTION" in
  on)
    [ "$(id -u)" = "0" ] || { echo "needs sudo: sudo bash $0 on"; exit 1; }
    pmset -a disablesleep 1
    echo "Sleep disabled. A closed lid will no longer stop it."
    if [ "$(power)" = "battery" ]; then
      LEFT=$(pmset -g batt | tail -1 | grep -o '[0-9]*:[0-9]*' | head -1)
      echo
      echo "WARNING: it is on BATTERY with about ${LEFT:-?} left, and it will now"
      echo "run flat rather than sleep. Plug it in."
    fi
    ;;
  off)
    [ "$(id -u)" = "0" ] || { echo "needs sudo: sudo bash $0 off"; exit 1; }
    pmset -a disablesleep 0
    echo "Sleep re-enabled. Closing the lid will stop the collector until you open it."
    echo "Nothing is lost when it does: a run reads 24h of new posts and a week of"
    echo "search, so a gap under a day catches itself up."
    ;;
  status)
    S=$(state); P=$(power)
    echo "power source:   $P"
    echo "battery:        $(pmset -g batt | tail -1 | sed 's/.*)//' | tr -s ' ')"
    echo "sleep disabled: ${S:-0}  ($([ "${S:-0}" = "1" ] && echo "stays awake, lid closed" || echo "sleeps normally"))"
    echo
    if [ "${S:-0}" = "1" ] && [ "$P" = "battery" ]; then
      echo "This is the risky combination: awake, unplugged, so it dies instead of"
      echo "napping. Either plug it in, or: sudo bash $0 off"
    elif [ "${S:-0}" = "1" ]; then
      echo "Good: plugged in and staying awake. Lid can be closed."
    else
      echo "It will sleep when the lid closes. For 24/7: sudo bash $0 on"
    fi
    ;;
  *) echo "usage: $0 [status|on|off]"; exit 1;;
esac
