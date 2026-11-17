#!/bin/bash
# Open the cockpit on THIS laptop. Works whether or not the scraper is running.
#
#   bash reddit/cockpit/cockpit.sh
#
# (`scratch/reddit/cockpit.sh` still works: it is a symlink to this file now.)
#
# It grabs anything new from the scraper first if that Mac happens to be awake,
# then serves the board from the local store. Nothing here waits on that machine.
set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
bash "$REPO/reddit/collector/sync.sh" || true
echo
exec python3 "$REPO/reddit/cockpit/server.py"
