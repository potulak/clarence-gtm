#!/bin/bash
# Bring a copy of the always-on machine's data here, to look at.
#
#   bash reddit/collector/pull-data.sh you@your-mac.local
#
# (`scratch/reddit/pull-data.sh` still works: it is a symlink to this file now.)
#
# One direction only, and it lands under a different name than a live store, so
# nothing here can be mistaken for the real record or accidentally sent back over
# it. The machine you pull from keeps collecting throughout, untouched.
set -e
TARGET="${1:-you@your-mac.local}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${SYNC_DEST:-clarence}"
OUT="$REPO/scratch/reddit/snapshot.db"

# A pull overwrites the local snapshot. If you have decisions in it that have not
# been handed back yet, that would silently destroy them, and they are the one
# thing here nothing can regenerate.
if [ -f "$OUT" ]; then
  PENDING=$(REDDIT_DB="$OUT" python3 -c "
import sys; sys.path.insert(0, '$REPO/reddit/collector')
import store
try:
    con = store.connect(); print(len(store.unpushed(con))); con.close()
except Exception: print(0)" 2>/dev/null || echo 0)
  if [ "${PENDING:-0}" != "0" ]; then
    echo "STOP: $PENDING decision(s) in the local snapshot have not been pushed yet."
    echo "Overwriting now would erase them. Send them first:"
    echo "    bash reddit/collector/push-actions.sh"
    exit 1
  fi
fi

if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$TARGET" true 2>/dev/null; then
  echo "Cannot reach $TARGET. Is it awake?"; exit 1
fi

# sqlite is mid-write sometimes, so ask sqlite for a consistent copy rather than
# grabbing the file underneath it.
echo "asking $TARGET for a clean copy…"
ssh "$TARGET" "cd '$DEST' && /usr/bin/python3 -c \"
import sqlite3
src = sqlite3.connect('scratch/reddit/reddit.db')
dst = sqlite3.connect('/tmp/clarence-snapshot.db')
src.backup(dst); dst.close(); src.close()
\""
rsync -q "$TARGET:/tmp/clarence-snapshot.db" "$OUT"
ssh "$TARGET" "rm -f /tmp/clarence-snapshot.db"

echo "landed: $OUT"
# The store module comes from the repo, the data from REDDIT_DB above. They are
# two different directories now, and the code one is NOT "next to the database".
CODE="$REPO/reddit/collector" REDDIT_DB="$OUT" /usr/bin/env python3 - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["CODE"])
import store
con = store.connect()
st = store.stats(con)
print(f"\n  {st['total']} posts across {st['subs']} subreddits")
print(f"  {st['judged']} judged, {st['kept']} worth answering")
print(f"  {st['posted']} posted by you, {st['skipped']} skipped")
rows = con.execute("SELECT subreddit, COUNT(*) n, SUM(COALESCE(keep,0)) k "
                   "FROM posts WHERE llm_score IS NOT NULL GROUP BY subreddit "
                   "ORDER BY k DESC, n DESC LIMIT 8").fetchall()
print("\n  best subreddits so far:")
for r in rows:
    print(f"    r/{r['subreddit'][:24]:24s} {r['k']} kept of {r['n']} read")
con.close()
PYEOF
echo
echo "Query it with anything that reads sqlite:"
echo "    sqlite3 $OUT 'select subreddit, title from posts where keep=1 limit 10'"
echo "    REDDIT_DB=$OUT python3 reddit/collector/monitor.py --subs"
