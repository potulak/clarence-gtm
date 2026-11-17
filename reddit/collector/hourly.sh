#!/bin/bash
# One pass of the Reddit loop. Run by launchd every hour, or by hand any time.
# Collects wide and cheap, judges only the budgeted top slice, republishes the
# queue as the top 10% of everything you have not acted on.
#
# This file is version controlled at reddit/collector/ and is also reachable at
# its old path, scratch/reddit/hourly.sh, which is now a symlink to it. Both sit
# two levels under the checkout, so the cd below lands on the repo root either
# way, and the launchd job keeps working without being rewritten.
cd "$(dirname "$0")/../.." || exit 1
mkdir -p scratch/reddit/logs
LOG="scratch/reddit/logs/$(date +%Y-%m-%d).log"
PY="$(command -v python3 || echo /usr/bin/python3)"
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
  # These two are reached through scratch/reddit, where they are now symlinks
  # into reddit/collector. A step guarded on `-f` would go quiet if its link
  # ever broke, because `-f` is false for a link pointing at nothing and a
  # skipped step looks like a step with nothing to do. So these two are checked
  # out loud instead: a missing file says so in the log.
  for step in monitor replies; do
    if [ -e "scratch/reddit/$step.py" ]; then
      "$PY" "scratch/reddit/$step.py"
    else
      echo "  ! scratch/reddit/$step.py is missing or its symlink is broken"
    fi
  done
  # Fill in what people actually wrote. The listing the collector reads carries
  # a title and no body, so without this the board is a pile of headlines and
  # the judge is reading "(not pulled)". Reads one subreddit feed a minute,
  # which is the measured limit, and stops itself well inside the hour. Never
  # fatal: a run with no bodies is worse than one with, and better than none.
  if [ -f reddit/collector/bodies.py ]; then
    echo "--- post bodies ---"
    "$PY" reddit/collector/bodies.py || echo "  ! bodies pass failed, titles only this hour"
  fi
  # Carry the result to the phone. Pull first so a decision made on the train is
  # folded in before the queue is recomputed, otherwise the post you just
  # triaged comes straight back. Never fatal: a failed sync must not take the
  # collector down with it, and the next run carries everything anyway.
  # Also a symlink into the repo, so it gets the same loud check: a quiet skip
  # here means the phone board stops updating and nothing says why.
  echo "--- cloud sync ---"
  if [ -e scratch/cloud/sync-cloud.py ]; then
    "$PY" scratch/cloud/sync-cloud.py || echo "  ! cloud sync failed, queue still local"
  else
    echo "  ! scratch/cloud/sync-cloud.py is missing or its symlink is broken"
  fi
  echo
} >> "$LOG" 2>&1
# Keep a fortnight of logs, no more.
find scratch/reddit/logs -name '*.log' -mtime +14 -delete 2>/dev/null
