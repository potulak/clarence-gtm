#!/bin/sh
# Fails if an em dash appears in any user-facing surface.
# Em dashes read as AI-generated text; the copy rule is real punctuation only
# (comma, period, colon, parentheses). See CLAUDE.md "Writing rules".
# Run from anywhere: resolves paths relative to the repo root.
# Usage: no-emdash.sh [site|mac|gtm|invite|all]   (default: all)
#
# Note on scope: `reddit/` is covered whole. It is the recruiting engine, and
# nearly all of it ends up in front of a person: the board in a browser, the
# prompts that write the Reddit comments and DMs the founder sends under his own
# name, the drafts those produce. Carving out "the plumbing" meant deciding file
# by file, and the file that quietly started writing copy was the one nobody
# re-checked.
#
# The three scrubbers that strip the character (`assist.no_emdash`,
# `collector/enrich.py`, `collector/monitor.py`) write it as a `\u2014` escape,
# so the gate can police the files without flagging the lines that enforce the
# rule. That is the pattern to copy anywhere else it is needed.
#
# The Android app predates this check and its comments carry the
# character throughout, so `android/` as a whole is deliberately NOT covered. The
# save-the-date campaign IS, because its prompt writes real text messages that go
# out from the founder's own number, which is the most exposed copy in the repo.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DASH="$(printf '\342\200\224')"

# Surfaces where the character is banned outright.
case "${1:-all}" in
  site) SCOPES="site/src site/public site/README.md site/package.json" ;;
  mac)  SCOPES="mac/src mac/package.json mac/electron-builder.yml mac/build/entitlements.mac.plist" ;;
  gtm)  SCOPES="docs/gtm" ;;
  invite) SCOPES="android/app/src/main/kotlin/dev/clarence/app/ai/InviteDraftService.kt android/app/src/main/kotlin/dev/clarence/app/ai/ClaudeInviteDraftService.kt android/app/src/main/kotlin/dev/clarence/app/ui/invite android/app/src/main/kotlin/dev/clarence/app/data/invite android/app/src/main/kotlin/dev/clarence/app/domain/invite" ;;
  *)    SCOPES="site/src site/public site/README.md site/package.json mac/src mac/package.json mac/electron-builder.yml mac/build/entitlements.mac.plist docs/gtm reddit android/app/src/main/kotlin/dev/clarence/app/ai/InviteDraftService.kt android/app/src/main/kotlin/dev/clarence/app/ai/ClaudeInviteDraftService.kt android/app/src/main/kotlin/dev/clarence/app/ui/invite android/app/src/main/kotlin/dev/clarence/app/data/invite android/app/src/main/kotlin/dev/clarence/app/domain/invite" ;;
esac

FOUND=0
for scope in $SCOPES; do
  path="$ROOT/$scope"
  [ -e "$path" ] || continue
  hits="$(grep -rnI "$DASH" "$path" 2>/dev/null \
    | grep -v node_modules | grep -v /dist/ | grep -v .wrangler \
    | grep -v __pycache__)"
  if [ -n "$hits" ]; then
    FOUND=1
    echo "$hits"
  fi
done

if [ "$FOUND" -eq 1 ]; then
  echo ""
  echo "FAIL: em dashes found in user-facing text (see lines above)."
  echo "Rewrite with a comma, period, colon, or parentheses. Not a hyphen."
  exit 1
fi

echo "OK: no em dashes in user-facing surfaces."
