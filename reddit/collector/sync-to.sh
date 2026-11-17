#!/bin/bash
# Copy the Reddit engine to your always-on Mac. The code lives under scratch/,
# which git ignores, so it does not arrive by pulling.
#
#   bash reddit/collector/sync-to.sh you@studio.local              # code only
#   bash reddit/collector/sync-to.sh you@studio.local --with-data  # FIRST TIME
#
# (scratch/reddit/sync-to.sh still works: it is a symlink to this file now.)
#
# Code only is the safe default, and it is what you want for every sync after the
# first. --with-data also carries the store across, which is right exactly once,
# when that machine takes over. Sending it again would overwrite everything that
# machine has collected since, including every posted and skipped decision you
# made, which is the one thing here that cannot be regenerated.
set -e
TARGET="$1"
[ -z "$TARGET" ] && { echo "usage: sync-to.sh user@host [--with-data]"; exit 1; }
# This file lives at reddit/collector/ and is also reachable at its old path,
# scratch/reddit/sync-to.sh, now a symlink to it. Both are two levels under the
# checkout, so this lands on the repo root either way.
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
# NOT ~/Documents: macOS blocks background agents from reading Documents,
# Desktop and Downloads, so a checkout there installs fine and then fails
# with "Operation not permitted" the moment launchd runs it.
DEST="${SYNC_DEST:-clarence}"

# 1. Can we even reach it? Fail here with something readable, not halfway through.
if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$TARGET" true 2>/dev/null; then
  echo "Cannot ssh to $TARGET."
  echo
  echo "On that Mac: System Settings > General > Sharing > Remote Login, turn it on."
  echo "Its address is shown right under the switch, something like you@studio.local."
  echo "Then try this once by hand:  ssh $TARGET"
  exit 1
fi

# 2. The cockpit reads the whole repo (site pages, git history for the schedule),
#    so a bare scratch/ folder gives you a working collector and a half-empty board.
ORIGIN="$(git -C "$REPO" remote get-url origin 2>/dev/null || echo '')"
if ! ssh "$TARGET" "test -d '$DEST/.git'" 2>/dev/null; then
  echo "$TARGET has no checkout at ~/$DEST yet."
  echo
  if [ -n "$ORIGIN" ]; then
    echo "Clone it there first, then run this again:"
    echo "    ssh $TARGET"
    echo "    git clone $ORIGIN $DEST"
  else
    echo "This repo has no origin remote, so copy the whole folder across instead"
    echo "(AirDrop a zip of it, or: rsync -av '$REPO/' '$TARGET:$DEST/')."
  fi
  echo
  echo "The Reddit collector alone would run without it, but the cockpit's other"
  echo "tabs read the repo and would come up empty."
  exit 1
fi

echo "sending the engine to $TARGET:$DEST"
ssh "$TARGET" "mkdir -p '$DEST/scratch/reddit' '$DEST/scratch/cockpit/web' \
  '$DEST/scratch/aeo' '$DEST/reddit/collector'"

# store.py, monitor.py and watch.json moved into the repo at reddit/collector on
# 2026-10-17, so that the scheduled GitHub Actions collector could import them.
# What is left in scratch/reddit are relative symlinks pointing at them, and
# `rsync -a` copies a symlink as a symlink, so this has to travel first or the
# links land on the Air pointing at nothing. Sending it here rather than relying
# on a `git pull` over there keeps this script the one way to move the engine.
rsync -av --exclude '__pycache__' \
  "$REPO/reddit/collector/" "$TARGET:$DEST/reddit/collector/"

# The cloud half moved into the repo the same night, for the same reason: the
# deployed Worker existed as one copy on one laptop. It travels the same way and
# has to arrive BEFORE scratch/cloud, because what is left over there are links
# into here. Guarded, so this script still works from a checkout that does not
# have it yet, which is every checkout until that branch is merged.
if [ -d "$REPO/reddit/cloud" ]; then
  ssh "$TARGET" "mkdir -p '$DEST/reddit/cloud'"
  rsync -av --exclude '__pycache__' --exclude '.wrangler' \
    "$REPO/reddit/cloud/" "$TARGET:$DEST/reddit/cloud/"
fi

# The board moved into the repo on 2026-10-17 too, the last of the three. Same
# ordering rule: what is left in scratch/cockpit are links into here, and
# `rsync -a` copies a link as a link, so this has to land first or they arrive
# pointing at nothing. Guarded like the others, so the script still works from a
# checkout that predates the move.
if [ -d "$REPO/reddit/cockpit" ]; then
  ssh "$TARGET" "mkdir -p '$DEST/reddit/cockpit/web'"
  rsync -av --exclude '__pycache__' \
    "$REPO/reddit/cockpit/" "$TARGET:$DEST/reddit/cockpit/"
fi

# The docs that describe the whole loop rather than one half of it.
rsync -av "$REPO/reddit/README.md" "$REPO/reddit/WHERE-IT-RUNS.md" \
  "$REPO/reddit/HANDOFF-cloud-and-phone.md" "$TARGET:$DEST/reddit/" 2>/dev/null || true

# The code and the watchlist. Never the secrets.
rsync -av --exclude '__pycache__' --exclude 'logs' --exclude 'backups' \
  --exclude 'reddit_creds.json' --exclude '*.db' --exclude 'queue.json' \
  --exclude 'state.json' --exclude 'seen.json' \
  "$REPO/scratch/reddit/" "$TARGET:$DEST/scratch/reddit/"
# scratch/cockpit now holds links into reddit/cockpit, which went across above,
# plus the ledger, which stays on the machine that owns it. Send the links, not
# the ledger: --with-data is the only thing allowed to move a record.
rsync -av --exclude '__pycache__' --exclude 'outreach.json' --exclude '*.log' \
  --exclude 'server.out' --exclude 'backups' \
  "$REPO/scratch/cockpit/" "$TARGET:$DEST/scratch/cockpit/"
rsync -av "$REPO/aeo/sources.json" "$TARGET:$DEST/aeo/" 2>/dev/null || true

# The cloud half, so whichever machine collects can also push to the phone.
# sync-cloud.py talks HTTPS with the cockpit key, so a collector needs no
# Cloudflare credentials at all. What is deliberately NOT sent:
#   .proxy-token     lives only as a Worker secret, the collector never calls it
#   .pull-watermark* per machine, and copying one would skip another's decisions
#   public/          only needed on the machine that runs `wrangler deploy`
ssh "$TARGET" "mkdir -p '$DEST/scratch/cloud'"
rsync -av --exclude '__pycache__' --exclude '.wrangler' --exclude 'public' \
  --exclude '.proxy-token' --exclude '.pull-watermark*' --exclude '.push.sql' \
  "$REPO/scratch/cloud/" "$TARGET:$DEST/scratch/cloud/"

if [ "$2" = "--with-data" ]; then
  echo
  # Only ask when there is actually something to destroy. On a machine that has
  # never collected, the store is arriving for the first time and there is no
  # decision of yours at risk. Asking there just trains you to say yes.
  if ssh "$TARGET" "test -s '$DEST/scratch/reddit/reddit.db'" 2>/dev/null; then
    echo "That machine ALREADY HAS a store. Sending yours would overwrite every"
    echo "posted and skipped decision it has recorded since, which cannot be rebuilt."
    read -r -p "Overwrite it anyway? [y/N] " ok
  else
    echo "That machine has no store yet, so this is the handover. Sending it."
    ok=y
  fi
  if [ "$ok" = "y" ] || [ "$ok" = "Y" ]; then
    rsync -av "$REPO/scratch/reddit/reddit.db" "$REPO/scratch/reddit/queue.json" \
      "$REPO/scratch/reddit/state.json" "$TARGET:$DEST/scratch/reddit/"
    echo "store sent. Do not pass --with-data again unless you mean to wipe that machine's record."
  else
    echo "left the store alone."
  fi
fi

# A running cockpit holds its Python modules in memory from startup, so new code
# on disk changes nothing until it restarts. Syncing without this looks like it
# worked and silently serves the old build.
if ssh "$TARGET" "test -f ~/Library/LaunchAgents/app.clarence.cockpit.plist" 2>/dev/null; then
  echo
  echo "restarting the board so it picks up the new code…"
  ssh "$TARGET" "launchctl kickstart -k gui/\$(id -u)/app.clarence.cockpit" 2>/dev/null \
    || ssh "$TARGET" "launchctl unload ~/Library/LaunchAgents/app.clarence.cockpit.plist; launchctl load ~/Library/LaunchAgents/app.clarence.cockpit.plist"
  sleep 2
  ssh "$TARGET" "lsof -nP -iTCP:4360 -sTCP:LISTEN >/dev/null 2>&1" \
    && echo "  board is back up" || echo "  WARNING: board did not come back, check its logs"
fi

echo
echo "Now, on that machine:"
echo "  1. put your key in ~/Library/Application Support/clarence-aeo/.env"
echo "  2. cd $DEST && bash scratch/reddit/setup-machine.sh"
echo
echo "Then turn the collector OFF here, so two machines are not building two"
echo "different piles from the same watchlist:"
echo "  launchctl unload ~/Library/LaunchAgents/app.clarence.reddit.hourly.plist"
