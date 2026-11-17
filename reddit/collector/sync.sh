#!/bin/bash
# Trade data with the scraper machine, in both directions, when it happens to be on.
#
#   bash reddit/collector/sync.sh
#
# (`scratch/reddit/sync.sh` still works: it is a symlink to this file now.)
#
# This laptop owns the store you work in. The other Mac only scrapes. So:
#   it -> here   the posts it found and the verdicts it paid for
#   here -> it   the decisions you made, so its ranking keeps learning
#
# Neither side overwrites the other, because they own different things. If that
# Mac is off, this exits quietly and everything here keeps working: the whole
# point is that your cockpit never waits on it.
set -e
TARGET="${1:-you@your-mac.local}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${SYNC_DEST:-clarence}"

if ! ssh -o ConnectTimeout=6 -o BatchMode=yes "$TARGET" true 2>/dev/null; then
  echo "Scraper is off. Nothing to trade, and nothing here depends on it."
  exit 0
fi
echo "Scraper is up. Trading…"

# --- it -> here: everything it has seen ---
ssh "$TARGET" "cd '$DEST' && python3 -c \"
import sys, json
sys.path.insert(0, 'reddit/collector')
import store
con = store.connect()
json.dump(store.export_posts(con), open('/tmp/clarence-posts.json', 'w'))
con.close()
\""
rsync -q "$TARGET:/tmp/clarence-posts.json" /tmp/clarence-posts.json
ssh "$TARGET" "rm -f /tmp/clarence-posts.json"
python3 -c "
import sys, json
sys.path.insert(0, '$REPO/reddit/collector')
import store
con = store.connect()
r = store.merge_posts(con, json.load(open('/tmp/clarence-posts.json')))
s = store.stats(con)
con.close()
print(f'  pulled in: {r[\"new\"]} new post(s), {r[\"verdicts_filled\"]} verdict(s) filled')
print(f'  you now hold {s[\"total\"]} posts, {s[\"kept\"]} worth answering')
"
rm -f /tmp/clarence-posts.json

# --- here -> it: your decisions, so its ranking learns from you ---
python3 -c "
import sys, json
sys.path.insert(0, '$REPO/reddit/collector')
import store
con = store.connect()
rows = store.unpushed(con)
json.dump(rows, open('/tmp/clarence-actions.json', 'w'))
con.close()
print(f'  sending back {len(rows)} decision(s)')
"
if [ -s /tmp/clarence-actions.json ]; then
  scp -q /tmp/clarence-actions.json "$TARGET:/tmp/clarence-actions.json"
  ssh "$TARGET" "cd '$DEST' && python3 -c \"
import sys, json
sys.path.insert(0, 'reddit/collector')
import store
con = store.connect()
rows = json.load(open('/tmp/clarence-actions.json'))
n = sum(1 for r in rows if store.apply_action(
    con, r['post_id'], r['status'], r['reason'], r['note'], r['at']))
con.close()
print('  applied over there:', n)
\"; rm -f /tmp/clarence-actions.json"
  python3 -c "
import sys
sys.path.insert(0, '$REPO/reddit/collector')
import store
con = store.connect()
store.mark_pushed(con, [r['rowid'] for r in store.unpushed(con)])
con.close()"
fi
rm -f /tmp/clarence-actions.json
echo "Done."
