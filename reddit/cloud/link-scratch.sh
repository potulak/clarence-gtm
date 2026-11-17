#!/bin/bash
# Point scratch/ at the copies in the repo, once this branch is merged.
#
#   bash reddit/cloud/link-scratch.sh                        # say what it would do
#   bash reddit/cloud/link-scratch.sh --apply                # do it
#   bash reddit/cloud/link-scratch.sh --apply --take-repo    # repo copy wins
#
# The engine moved into the repo in three passes, for one reason: it was one
# copy on one laptop, and scratch/ is gitignored in full. reddit/collector/ took
# the collector, reddit/cloud/ the Worker and the phone board, reddit/cockpit/
# the local board on 4360. This is the other half of all three: scratch/ keeps
# the paths everything already calls, as relative symlinks, so there is one copy
# and the two Macs cannot drift.
#
# It still lives under cloud/ because that is the path docs/URGENT.md and the
# founder's half-finished flip already name. It covers the whole engine.
#
# Run it on BOTH Macs, and only after the repo here actually has reddit/cloud,
# which on the collector means after `sync-to.sh` has carried it over. It
# refuses rather than leaving a link pointing at nothing, it never touches data
# or secrets, and running it twice does nothing the second time.
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APPLY=""
TAKE_REPO=""
for arg in "$@"; do
  case "$arg" in
    --apply)     APPLY=1 ;;
    --take-repo) TAKE_REPO=1 ;;
    *) echo "unknown option: $arg"; exit 1 ;;
  esac
done
# One stamp per pass, so a backup folder says which move it came from. The
# earlier three are pre-collector-, pre-cloud- and pre-cockpit-move-2026-10-17.
STAMP="pre-scripts-move-2026-10-17"

# scratch path : repo path : how many levels up from the scratch file to the root
LINKS="
scratch/cloud/README.md:reddit/cloud/README.md:../..
scratch/cloud/link.sh:reddit/cloud/link.sh:../..
scratch/cloud/schema.sql:reddit/cloud/schema.sql:../..
scratch/cloud/sync-cloud.py:reddit/cloud/sync-cloud.py:../..
scratch/cloud/wrangler.toml:reddit/cloud/wrangler.toml:../..
scratch/cloud/src/worker.js:reddit/cloud/src/worker.js:../../..
scratch/cloud/public/manifest.webmanifest:reddit/cloud/public/manifest.webmanifest:../../..
scratch/cloud/public/icon-192.png:reddit/cloud/public/icon-192.png:../../..
scratch/cloud/public/icon-512.png:reddit/cloud/public/icon-512.png:../../..
scratch/cockpit/web/m.html:reddit/cloud/public/m.html:../../..
scratch/reddit/replies.py:reddit/collector/replies.py:../..
scratch/reddit/set-inbox-feed.sh:reddit/collector/set-inbox-feed.sh:../..
scratch/cockpit/server.py:reddit/cockpit/server.py:../..
scratch/cockpit/outreach.py:reddit/cockpit/outreach.py:../..
scratch/cockpit/web/index.html:reddit/cockpit/web/index.html:../../..
scratch/reddit/enrich.py:reddit/collector/enrich.py:../..
scratch/reddit/shortlist.py:reddit/collector/shortlist.py:../..
scratch/reddit/hourly.sh:reddit/collector/hourly.sh:../..
scratch/reddit/sync-to.sh:reddit/collector/sync-to.sh:../..
scratch/reddit/README.md:reddit/README.md:../..
scratch/reddit/WHERE-IT-RUNS.md:reddit/WHERE-IT-RUNS.md:../..
scratch/reddit/HANDOFF-cloud-and-phone.md:reddit/HANDOFF-cloud-and-phone.md:../..
scratch/reddit/archived.py:reddit/collector/archived.py:../..
scratch/reddit/awake.sh:reddit/collector/awake.sh:../..
scratch/reddit/pull-data.sh:reddit/collector/pull-data.sh:../..
scratch/reddit/push-actions.sh:reddit/collector/push-actions.sh:../..
scratch/reddit/push-repo.sh:reddit/collector/push-repo.sh:../..
scratch/reddit/setup-machine.sh:reddit/collector/setup-machine.sh:../..
scratch/reddit/sync.sh:reddit/collector/sync.sh:../..
scratch/reddit/cockpit.sh:reddit/cockpit/cockpit.sh:../..
scratch/reddit/offline.sh:reddit/cockpit/offline.sh:../..
"

# NOT in that list, on purpose:
#
#   scratch/cloud/public/m.html   the staged deploy copy, not the source.
#                                 sync-cloud.py writes it on every push so a
#                                 deploy cannot ship a stale fork of the board.
#                                 Linking it would make that staging inert.
#   .proxy-token, .pull-watermark*, .wrangler/, reddit.db, queue.json,
#   seen.json, state.json, reddit_creds.json, icp_corpus.jsonl, outreach.json,
#   snapshot.db, logs/, backups/
#                                 data and secrets. They never go in the repo,
#                                 which is the whole reason the split exists.
#   scratch/reddit/monitor_public.py
#                                 dead. It is the ancestor of monitor.py, which
#                                 does everything it does and more, and nothing
#                                 calls it. It also writes queue.json into the
#                                 working directory, so running it from here
#                                 would flatten the live queue. Kept on the
#                                 laptop as history rather than committed.

missing=0
for row in $LINKS; do
  repo="$(echo "$row" | cut -d: -f2)"
  [ -f "$ROOT/$repo" ] || { echo "  missing in the repo: $repo"; missing=1; }
done
if [ "$missing" = 1 ]; then
  echo
  echo "This checkout does not have the cloud half yet, so there is nothing to"
  echo "point at. Merge the branch that adds reddit/cloud/ and pull, or on the"
  echo "collector run sync-to.sh from the laptop, then run this again."
  echo "Nothing was changed."
  exit 1
fi

changed=0
for row in $LINKS; do
  scratch="$(echo "$row" | cut -d: -f1)"
  repo="$(echo "$row" | cut -d: -f2)"
  up="$(echo "$row" | cut -d: -f3)"
  target="$up/$repo"
  live="$ROOT/$scratch"

  # Nothing there at all: on this machine that file is simply not used (the
  # collector has no public/, for instance). Leave it alone rather than
  # inventing a copy of the deploy tree where nothing deploys.
  if [ ! -e "$live" ] && [ ! -L "$live" ]; then
    echo "  skip     $scratch (not on this machine)"
    continue
  fi

  if [ -L "$live" ]; then
    if [ "$(readlink "$live")" = "$target" ]; then
      echo "  already  $scratch"
    else
      echo "  OTHER LINK $scratch points at $(readlink "$live"), not $target"
      echo "             Left alone. Look at it by hand."
    fi
    continue
  fi

  # A real file that differs from the repo copy is an edit made since the move,
  # and replacing it with a link would throw that edit away silently. That is
  # the one failure this script must never cause.
  #
  # Except on the move itself. A pass that also FIXES a file as it lifts it into
  # the repo (paths that have to be reworked, copy that has to pass the em-dash
  # gate) leaves every one of those files differing, which is this branch for
  # the wrong reason. --take-repo says "I know, the repo copy is the new one".
  # It still takes the backup, so the scratch copy is recoverable either way.
  if ! cmp -s "$live" "$ROOT/$repo"; then
    if [ -z "$TAKE_REPO" ]; then
      echo "  DIFFERS  $scratch is not the same as $repo"
      echo "           Left alone. Compare them, put the good one in the repo,"
      echo "           commit it, then run this again:"
      echo "             diff $ROOT/$repo $live"
      echo "           If the repo copy is the newer one (a move that also"
      echo "           changed the file), say so: --apply --take-repo"
      continue
    fi
    if [ -z "$APPLY" ]; then
      echo "  would REPLACE $scratch (differs) -> $target"
      changed=1
      continue
    fi
    echo "  differs, taking the repo copy: $scratch"
  fi

  if [ -z "$APPLY" ]; then
    echo "  would link $scratch -> $target"
    changed=1
    continue
  fi

  # One backup folder per area, keeping the shape underneath it, rather than a
  # backups/ dir sprinkled into src/ and public/ as well.
  area="$(echo "$scratch" | cut -d/ -f2)"
  rel="${scratch#scratch/$area/}"
  backup="$ROOT/scratch/$area/backups/$STAMP/$(dirname "$rel")"
  mkdir -p "$backup"
  cp -p "$live" "$backup/$(basename "$scratch")"
  rm "$live"
  ln -s "$target" "$live"
  echo "  linked   $scratch -> $target"
  changed=1
done

echo
if [ -z "$APPLY" ]; then
  if [ "$changed" = 1 ]; then
    echo "That was a dry run. Run it again with --apply to do it."
  else
    echo "Nothing to do here: already linked, or none of it runs on this machine."
  fi
  exit 0
fi

# Prove it, rather than trusting the output above. A link that resolves to
# nothing is the failure mode worth catching, and `-f` is false for a broken
# symlink, which is how a dead link turns into a feature that silently stopped
# running rather than an error anyone sees.
bad=0
for row in $LINKS; do
  scratch="$(echo "$row" | cut -d: -f1)"
  live="$ROOT/$scratch"
  [ -L "$live" ] || continue
  [ -f "$live" ] || { echo "  BROKEN: $scratch"; bad=1; }
done
if [ "$bad" = 1 ]; then
  echo
  echo "At least one link does not resolve. The originals are in the backups/"
  echo "folder beside each one, under $STAMP."
  exit 1
fi
echo "All links resolve. Originals kept in scratch/*/backups/$STAMP."
echo
echo "Worth running once, here, to see it work rather than assume it:"
echo "  python3 scratch/cloud/sync-cloud.py --help"
echo "  python3 scratch/reddit/replies.py --list | head"
