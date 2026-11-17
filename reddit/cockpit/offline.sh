#!/bin/bash
# Work the queue on this laptop while the always-on Mac is shut.
#
#   bash reddit/cockpit/offline.sh          # pull a copy and open the board here
#
# (`scratch/reddit/offline.sh` still works: it is a symlink to this file now.)
#
# It serves the SAME cockpit against a local snapshot, on port 4361 so it can
# never be confused with the real one. Read it, skip things, mark things posted.
# Your decisions are journalled locally and handed back with push-actions.sh the
# next time that Mac is awake.
#
# What it cannot do here: collect new posts or judge them. Those belong to the
# machine that owns the store, on purpose, so there is only ever one of it.
set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SNAP="$REPO/scratch/reddit/snapshot.db"
TARGET="${1:-you@your-mac.local}"

if ssh -o ConnectTimeout=6 -o BatchMode=yes "$TARGET" true 2>/dev/null; then
  echo "That Mac is awake. Pushing anything queued, then refreshing your copy…"
  bash "$REPO/reddit/collector/push-actions.sh" "$TARGET" || true
  bash "$REPO/reddit/collector/pull-data.sh" "$TARGET" || true
  echo
  echo "It is up, so you may as well use the real board:"
  echo "    http://your-mac.local:4360"
  echo
else
  echo "That Mac is not reachable, working from your local copy."
  [ -f "$SNAP" ] || { echo "No snapshot yet. Pull one while it is awake first."; exit 1; }
  AGE=$(python3 -c "
import os, time
print(int((time.time() - os.path.getmtime('$SNAP')) / 3600))")
  echo "snapshot is ${AGE}h old"
fi

echo "Opening the board on this Mac at http://localhost:4361"
echo "Decisions you make are queued. Send them over with:"
echo "    bash reddit/collector/push-actions.sh"
echo
REDDIT_DB="$SNAP" CLARENCE_ORIGIN="laptop" COCKPIT_PORT=4361 \
  python3 "$REPO/reddit/cockpit/server.py"
