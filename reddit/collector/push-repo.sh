#!/bin/bash
# Put this repo on your other Mac without GitHub, credentials, or a 9GB copy.
#
#   bash reddit/collector/push-repo.sh you@thatmac.local
#
# (`scratch/reddit/push-repo.sh` still works: it is a symlink to this file now.)
#
# The ordinary route is `git clone`, which is what reddit/README.md tells you to
# do and what the Air actually did. This is the fallback for a Mac with no
# GitHub auth on it yet: same history, nothing to sign in to.
#
# It sends the git history as a single bundle file (about 26MB), clones from that
# on the far side, and points origin back at GitHub so pull and push work later
# once you have auth sorted there. A bundle is an atomic snapshot, which is the
# safe way to do this: copying a live .git directory can catch it mid-write.
set -e
TARGET="$1"
[ -z "$TARGET" ] && { echo "usage: push-repo.sh user@host"; exit 1; }
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
# NOT ~/Documents: macOS blocks background agents from reading Documents,
# Desktop and Downloads, so a checkout there installs fine and then fails
# with "Operation not permitted" the moment launchd runs it.
DEST="${SYNC_DEST:-clarence}"
ORIGIN="$(git -C "$REPO" remote get-url origin 2>/dev/null || echo '')"
BUNDLE="/tmp/clarence-$(date +%s).bundle"

if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$TARGET" true 2>/dev/null; then
  echo "Cannot ssh to $TARGET."
  echo "On that Mac: System Settings > General > Sharing > Remote Login, turn it on."
  echo "The address is under the switch, like you@studio.local."
  exit 1
fi

if ssh "$TARGET" "test -e '$DEST'" 2>/dev/null; then
  echo "$TARGET already has something at ~/$DEST. Not touching it."
  echo "Move it aside first, or set SYNC_DEST to a different folder."
  exit 1
fi

echo "packing the history (this does not include the 9GB of untracked files)…"
git -C "$REPO" bundle create "$BUNDLE" --all
du -h "$BUNDLE" | awk '{print "  bundle: "$1}'

echo "sending it…"
scp -q "$BUNDLE" "$TARGET:/tmp/clarence.bundle"

echo "unpacking on the far side…"
ssh "$TARGET" "set -e
  git clone -q /tmp/clarence.bundle '$DEST'
  cd '$DEST'
  git remote set-url origin '${ORIGIN:-https://github.com/potulak/clarence.git}'
  rm -f /tmp/clarence.bundle
  echo '  checked out: '\$(git rev-parse --abbrev-ref HEAD)' at '\$(git log -1 --format=%h)"
rm -f "$BUNDLE"

echo
echo "Repo is on $TARGET at ~/$DEST, with its history and origin pointing at GitHub."
echo "Next, send the parts git ignores:"
echo "    bash reddit/collector/sync-to.sh $TARGET --with-data"
