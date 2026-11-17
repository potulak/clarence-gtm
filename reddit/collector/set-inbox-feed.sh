#!/bin/bash
# Store a private Reddit replies feed, from the clipboard.
#
# There are TWO worth having, and they catch different things:
#
#   "comment replies only"    someone answered a comment you left on
#                             somebody else's thread. This is the one that
#                             matters most, because answering other people's
#                             threads is how the recruiting actually works.
#   "post replies only"       someone commented on a thread YOU posted.
#
# Run this once per link. It works out which kind it is from the URL and keeps
# both, so the second run does not overwrite the first.
#
#   1. reddit.com/prefs/feeds, logged in as the account you post from
#   2. right-click the RSS button next to one of those two > Copy Link
#   3. bash scratch/reddit/set-inbox-feed.sh
#   4. copy the other link, run it again
#
# It reads the clipboard, so the URL never has to be typed, pasted into a chat,
# or shown on screen. Each URL is a password for your Reddit inbox: anyone
# holding it can read those replies. They go into reddit_creds.json, which is
# gitignored and mode 600, and this script never prints them back.
#
# Changing your Reddit password invalidates BOTH feeds, because the token is per
# account. Re-run this twice if that happens.
set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
# Overridable so this can be exercised without writing to the real credential
# file. Nothing but a test should ever set it.
CREDS="${CLARENCE_CREDS:-$REPO/scratch/reddit/reddit_creds.json}"

# This script NEVER waits for input. An earlier version prompted for a hidden
# paste, and a hidden cursor with no echo is indistinguishable from a hang, so it
# read as a crash every time. Three non-interactive ways in instead:
#
#   bash set-inbox-feed.sh                 # from the clipboard
#   bash set-inbox-feed.sh <url>           # straight in (lands in shell history)
#   bash set-inbox-feed.sh --file <path>   # from a file you pasted it into
#
URL=""
case "${1:-}" in
  --file)
    if [ -z "${2:-}" ] || [ ! -f "$2" ]; then
      echo "Usage: bash $0 --file /path/to/file-with-the-link" >&2
      exit 1
    fi
    URL="$(tr -d '[:space:]' < "$2")"
    SOURCE="the file $2"
    ;;
  "")
    URL="$(pbpaste 2>/dev/null | tr -d '[:space:]')"
    SOURCE="the clipboard"
    ;;
  *)
    URL="$(printf '%s' "$1" | tr -d '[:space:]')"
    SOURCE="the command line"
    ;;
esac

case "$URL" in
  https://*reddit.com/message/*feed=*) ;;         # good, carry on
  *)
    echo "No usable feed link in $SOURCE."
    echo
    echo "It needs to look like one of these:"
    echo "  https://www.reddit.com/message/comments/.rss?feed=<token>&user=<you>"
    echo "  https://www.reddit.com/message/selfreply/.rss?feed=<token>&user=<you>"
    echo
    echo "Pick whichever is easiest:"
    echo
    echo "  1. CLIPBOARD. On reddit.com/prefs/feeds, right-click the RSS button"
    echo "     next to 'comment replies only' (or 'post replies only') and Copy"
    echo "     Link. Then run this again WITHOUT copying anything else in between."
    echo
    echo "  2. FILE. Paste the link into a new TextEdit document, save it as"
    echo "     plain text to your Desktop as feed.txt, then run:"
    echo "       bash $0 --file ~/Desktop/feed.txt"
    echo "     (delete feed.txt afterwards, it holds the token)"
    echo
    echo "  3. DIRECTLY. Paste it after the command:"
    echo "     bash $0 'https://www.reddit.com/message/comments/.rss?feed=...'"
    echo "     This one lands in your shell history, so clear it after if you mind."
    echo
    echo "Nothing was changed."
    exit 1
    ;;
esac

python3 - "$CREDS" "$URL" <<'PY'
import json, sys, urllib.parse

creds_path, url = sys.argv[1], sys.argv[2]
u = urllib.parse.urlparse(url)
q = urllib.parse.parse_qs(u.query)

problems = []
if u.scheme != "https" or not u.netloc.endswith("reddit.com"):
    problems.append("that is not a reddit.com https link")
if "/message/" not in u.path:
    problems.append("that is not an inbox feed (the path has no /message/)")
if not q.get("feed"):
    problems.append("no feed token in it")
if not q.get("user"):
    problems.append("no username in it")

# Which section this is, taken from the path rather than asked for, so one
# command handles all of them and the order you paste them in does not matter.
# Any /message/<section>/ feed Reddit offers is accepted; these are just the
# names for the ones worth having, so this file does not have to be kept in step
# with whatever that settings page is called this year.
KINDS = {
    "comments": "comment replies (someone answered a comment you left)",
    "selfreply": "post replies (someone commented on a thread you posted)",
    "mentions": "mentions of your username",
    "messages": "private messages",
    "unread": "anything unread",
    "inbox": "the whole inbox",
}
# The two that matter most for recruiting, so a missing one is worth saying.
IMPORTANT = ("comments", "selfreply")

# Reddit is not consistent about where the .rss goes. Comment replies come as
# /message/comments/.rss, with the suffix as its own path segment, but post
# replies come as /message/selfreply.rss, with it glued to the section name.
# Taking the segment naively stored that one as "selfreply.rss", which then
# matched no known section: it still fetched, but it was labelled as an unknown
# section, sorted last behind the catch-all `unread` feed, and the setup
# checklist kept insisting post replies were missing when they were not.
seg = [s for s in u.path.split("/") if s and s != ".rss"]
kind = seg[1] if len(seg) > 1 and seg[0] == "message" else None
if kind and kind.endswith(".rss"):
    kind = kind[: -len(".rss")]
if not kind:
    problems.append(
        f"that is not an inbox section feed. The path is '{u.path}'; it needs "
        f"to look like /message/comments/.rss or /message/selfreply/.rss")
label = KINDS.get(kind, f"the '{kind}' section of your inbox")

if problems:
    print("That clipboard content does not look like the right link:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    print("\nGo to reddit.com/prefs/feeds and right-click the RSS button next to",
          file=sys.stderr)
    print('"comment replies only" or "post replies only", then Copy Link, then',
          file=sys.stderr)
    print("run this again.", file=sys.stderr)
    raise SystemExit(1)

user = q["user"][0]
try:
    creds = json.load(open(creds_path))
except Exception:
    creds = {}

feeds = creds.get("inbox_feeds") or {}
replacing = kind in feeds
# Clear out the mis-keyed version an earlier run of this script could have
# written (e.g. "selfreply.rss" instead of "selfreply"), so re-pasting the same
# link is all it takes to repair it rather than hand-editing the file.
stale = feeds.pop(kind + ".rss", None)
feeds[kind] = url
creds["inbox_feeds"] = feeds
# Kept alongside the new map on purpose. The always-on Mac may still be running
# an older replies.py that only knows `inbox_rss`, and leaving this set means it
# keeps fetching comment replies hourly instead of silently fetching nothing
# until the new code reaches it.
if kind == "comments":
    creds["inbox_rss"] = url
creds["reddit_username"] = user
with open(creds_path, "w") as f:
    json.dump(creds, f, indent=2)
    f.write("\n")

print(f"  {'replaced' if replacing else 'saved'}. account: u/{user}")
if stale:
    print(f"  (cleaned up an earlier mis-keyed '{kind}.rss' entry for the same feed)")
print(f"  this link: {label}")
print(f"  stored in: {creds_path} (gitignored, mode 600, never printed)")
print()
print("  feeds now set:")
for k, lbl in KINDS.items():
    print(f"    [{'yes' if k in feeds else '   '}] {lbl}")
for k in feeds:
    if k not in KINDS:
        print(f"    [yes] the '{k}' section of your inbox")
missing = [KINDS[k] for k in IMPORTANT if k not in feeds]
if missing:
    print()
    print("  Worth adding: " + missing[0])
    print("  Copy that link from reddit.com/prefs/feeds and run this again.")
print()
print("  NEXT, and only once, so your existing inbox does not arrive as a pile")
print("  of things to answer:")
print("    python3 scratch/reddit/replies.py --seed")
PY

chmod 600 "$CREDS"

# The collector is the machine that actually fetches this every hour, so the
# credential has to live there too. Doing it here means "set it up" is one
# command rather than one command plus a thing someone has to remember.
COLLECTOR="${COLLECTOR:-you@your-mac.local}"
echo
if ssh -o ConnectTimeout=6 -o BatchMode=yes "$COLLECTOR" true 2>/dev/null; then
  if scp -q "$CREDS" "$COLLECTOR:~/clarence/scratch/reddit/reddit_creds.json" 2>/dev/null; then
    ssh "$COLLECTOR" "chmod 600 ~/clarence/scratch/reddit/reddit_creds.json" 2>/dev/null
    echo "  also copied to the collector ($COLLECTOR), which is what fetches hourly"
  else
    echo "  ! could not copy to $COLLECTOR. Replies will only update when you run"
    echo "    scratch/reddit/replies.py on this machine."
  fi
else
  echo "  the collector ($COLLECTOR) is asleep, so this machine has it but that one"
  echo "  does not yet. Run this again when it is awake, or replies will only"
  echo "  update when you run scratch/reddit/replies.py here."
fi

echo
echo "That is the whole setup. From here the hourly job fetches replies on its"
echo "own. The only thing that breaks it is changing your Reddit password, which"
echo "invalidates the feed; if that happens, run this once more."
echo
echo "Now clear your clipboard, since it may still hold the token:"
echo "  printf '' | pbcopy"
