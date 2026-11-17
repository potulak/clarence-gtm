#!/bin/bash
# Hand the decisions you made here back to the always-on machine.
#
#   bash reddit/collector/push-actions.sh
#
# (`scratch/reddit/push-actions.sh` still works: it is a symlink to this file.)
#
# Work the queue on this laptop while the other Mac is shut, then run this when
# it is awake. Only your decisions travel: status, why you skipped, your note.
# The collector's own columns (scores, verdicts, what it has seen) are never
# touched, which is why two machines can hold the same store without fighting.
# Replay is last-write-wins by the time a decision was MADE, so a queued action
# can never overwrite a newer one, and running this twice is harmless.
set -e
TARGET="${1:-you@your-mac.local}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${SYNC_DEST:-clarence}"
SNAP="$REPO/scratch/reddit/snapshot.db"

[ -f "$SNAP" ] || { echo "No local snapshot at $SNAP. Nothing to push."; exit 0; }

PENDING=$(REDDIT_DB="$SNAP" python3 -c "
import sys; sys.path.insert(0, '$REPO/reddit/collector')
import store; con = store.connect(); print(len(store.unpushed(con))); con.close()")
if [ "$PENDING" = "0" ]; then echo "Nothing waiting to push."; exit 0; fi
echo "$PENDING decision(s) waiting."

if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$TARGET" true 2>/dev/null; then
  echo "Cannot reach $TARGET, so they stay queued here. Run this again when it is awake."
  exit 1
fi

REDDIT_DB="$SNAP" python3 -c "
import sys, json
sys.path.insert(0, '$REPO/reddit/collector')
import store
con = store.connect()
rows = store.unpushed(con)
print(json.dumps(rows))
con.close()" > /tmp/clarence-actions.json

scp -q /tmp/clarence-actions.json "$TARGET:/tmp/clarence-actions.json"
APPLIED=$(ssh "$TARGET" "cd '$DEST' && python3 -c \"
import sys, json
sys.path.insert(0, 'reddit/collector')
import store
con = store.connect()
rows = json.load(open('/tmp/clarence-actions.json'))
n = sum(1 for r in rows if store.apply_action(
    con, r['post_id'], r['status'], r['reason'], r['note'], r['at']))
con.close()
print(n)
\"; rm -f /tmp/clarence-actions.json")

echo "applied on the far side: $APPLIED of $PENDING"
REDDIT_DB="$SNAP" python3 -c "
import sys; sys.path.insert(0, '$REPO/reddit/collector')
import store
con = store.connect()
store.mark_pushed(con, [r['rowid'] for r in store.unpushed(con)])
con.close()
print('local journal cleared')"
rm -f /tmp/clarence-actions.json
echo
echo "Skipped ones were already decided more recently over there. That is the"
echo "merge rule working, not an error."
