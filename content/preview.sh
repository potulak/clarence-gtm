#!/usr/bin/env bash
# Preview un-merged content drafts on your local site so you can read them as
# real, rendered pages before merging, instead of squinting at a GitHub diff.
#
#   bash content/preview.sh          # pull every open draft into the local site
#   bash content/preview.sh --clean  # remove the un-merged preview files again
#
# Then run the site dev server and open the printed localhost URLs. Drafts that
# are already merged to main are skipped (they render on their own).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
ANSWERS="site/src/content/answers"
MARK="$ANSWERS/.preview-manifest"

if [ "${1:-}" = "--clean" ]; then
  if [ -f "$MARK" ]; then
    while IFS= read -r f; do [ -n "$f" ] && rm -f "$f"; done < "$MARK"
    rm -f "$MARK"
    echo "removed preview files."
  else
    echo "nothing to clean."
  fi
  exit 0
fi

git fetch origin --quiet
: > "$MARK"
found=0
for b in $(git branch -r --list 'origin/content/answer-*' | sed 's/^[* ]*//'); do
  f=$(git ls-tree -r --name-only "$b" -- "$ANSWERS" 2>/dev/null | grep '\.md$' | head -1 || true)
  [ -z "$f" ] && continue
  # skip drafts already merged to main (they render without a preview copy)
  git cat-file -e "origin/main:$f" 2>/dev/null && continue
  git show "$b:$f" > "$f"
  echo "$f" >> "$MARK"
  slug=$(basename "$f" .md)
  echo "  draft ready:  http://localhost:4321/answers/$slug"
  found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
  echo "no un-merged content drafts to preview."
  rm -f "$MARK"
else
  echo ""
  echo "$found draft(s) pulled. Start the site dev server, then open the URLs above."
  echo "Index of all pages: http://localhost:4321/answers"
  echo "When done reviewing, run: bash content/preview.sh --clean"
fi
