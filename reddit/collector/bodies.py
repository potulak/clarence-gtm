#!/usr/bin/env python3
"""Fill in what people actually wrote, so the board can show the post.

    python3 reddit/collector/bodies.py              # one pass, the day's subs
    python3 reddit/collector/bodies.py --subs 12    # go wider
    python3 reddit/collector/bodies.py --dry-run    # say what it would read

Run from the hourly job, between collecting and pushing to the phone.

WHY THIS EXISTS
---------------
The collector reads subreddits through `multireddit_new`, which parses a
listing page. A listing page carries a title, a reply count and a timestamp,
and no body at all: `parse_listing` returns `"body": ""` and says so. So the
store fills up with posts nobody can read without opening Reddit, which is the
one thing the board is for. Measured on 2026-10-17, all ten of the day's picks
had under 330 characters of body, and most had none.

Reading the thread itself is no longer possible logged out. Measured from the
always-on Mac, on a home connection, the same evening:

    www.reddit.com/<thread>/.rss     403 Blocked, every time, however patient
    www.reddit.com/<thread>/.json    403 Blocked
    old.reddit.com/<thread>          200, and it is the login page
    old.reddit.com/r/<sub>/new/.rss  200, and it is the login page

So `enrich.scrape_thread`, and the board's "Read the full post" button with it,
cannot work on any machine right now. Not the phone, not the laptop, not the
always-on Mac. That is not a datacenter problem and a residential address does
not fix it.

ONE path still answers, and it is the one the collector was already closest to:

    www.reddit.com/r/<sub>/new/.rss?limit=100    200, full bodies, every entry

A subreddit's own Atom feed puts the whole selftext in `<content>`. One request
returns up to a hundred of them. `monitor._feed_entry` already parses exactly
this shape, because the older `collect_rss` path read these feeds and then threw
the body away at 280 characters.

So the unit of spend here is a SUBREDDIT, not a thread, which is what makes it
affordable.

THE BUDGET, measured rather than guessed
----------------------------------------
That feed allows one read per sixty seconds per address. Measured by polling one
feed every twenty seconds: answered at 1s, 429 at 21s and 41s, answered at 62s,
429 at 82s and 102s, answered at 123s. Exactly one read a minute, cleanly.

So the number of subreddits per pass is the number of minutes this is allowed to
take. `--subs 8` is the default, which is eight minutes of mostly sleeping inside
an hourly job, and it is chosen against the shape of the shortlist rather than
the shape of the limit: the day's picks are capped at four per subreddit
(`shortlist.PER_SUB`), and on a real day they arrive from about five. Measured
2026-10-17: ten picks from five subreddits. Eight covers that with room, and
`MAX_MINUTES` stops an unusual day from ever running into the next hourly pass.

Nothing here is wasted on posts you will not see. The subreddits are chosen
BECAUSE the day's shortlist draws from them, highest demand first.

WHAT THE FEED WINDOW DOES AND DOES NOT COVER
--------------------------------------------
`limit=100` is honoured: measured on r/FriendshipAdvice, 100 entries, 310KB, 96
of them with more than 280 characters of body, the longest 9872. In that
subreddit, the busiest the shortlist draws from, those hundred posts reached back
20.7 hours. A quiet subreddit reaches back much further.

So a pass fills in anything up to about a day old in a busy subreddit, and
`shortlist.rank` weights freshness against a 96 hour horizon, which means a pick
is almost always this day or yesterday: every one of the ten measured was under
35 hours old. Running hourly closes the gap, because a post that is 33 hours old
now was 9 hours old on the pass 24 runs ago, and bodies are kept once written.

The one thing this cannot do is reach back for a post that was already old the
first time this ever ran. Those stay as they are, and the board still offers the
paste box for them.

AND THE JUDGE GETS IT FOR FREE
------------------------------
One read hands over a hundred bodies and the picks need a handful, so every other
id in that feed is filled in at no extra cost. Those are mostly posts the judge
has not reached yet, because judging is capped per run. `_triage_batch` prefers
`post_text` when there is one, so the next run judges them on what the person
wrote instead of on "(not pulled, judge from the title and the subreddit)".

This only ever writes `post_text`. It does not touch status, scores, verdicts or
anything else you decided, so it is safe to run next to everything else and safe
to run twice.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import monitor  # noqa: E402
import store  # noqa: E402

# `shortlist.py` is not version controlled and lives beside the data rather than
# beside this file, so it is reached through the same path store works out for
# itself. A runner with no `scratch/` simply has no shortlist, and `wanted_subs`
# falls back to where the unreadable posts are instead of refusing to run.
sys.path.append(str(store.DATA))

# One read per sixty seconds, measured. The extra five is margin: being thrown a
# 429 costs a whole minute of waiting, so it is cheaper to be slightly late than
# slightly early.
GAP = 65.0
# Enough for the shortlist's real spread, with room. See the budget note above.
SUBS = 8
# A hard ceiling, so a day with an unusual spread can never still be reading
# when the next hourly pass starts.
MAX_MINUTES = 15.0
# How many of a subreddit's newest posts to ask for. The feed's own cap.
PER_FEED = 100
# Below this a body is a listing preview that trailed off, not a post. Same
# number the board uses to decide a card is unreadable.
SHORT = 330


class Throttled(RuntimeError):
    """The feed said 429. Clears on its own, in about a minute."""


def read_feed(sub: str, limit: int = PER_FEED) -> list[dict]:
    """One subreddit's newest posts, with their bodies. One request."""
    url = ("https://www.reddit.com/r/%s/new/.rss?limit=%d"
           % (urllib.parse.quote(sub), limit))
    req = urllib.request.Request(url, headers={"User-Agent": monitor.RSS_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise Throttled("r/%s: 429" % sub) from e
        raise
    if "welcome to reddit" in xml[:3000].lower():
        # www answers the feed, old.reddit answers the login page. If that ever
        # changes for www too, say so rather than filing a login page as a post.
        raise RuntimeError("r/%s: the feed returned a login page" % sub)
    return [monitor._feed_entry(b) for b in monitor._ENTRY.findall(xml)]


def wanted_subs(con, limit: int = SUBS) -> list[tuple[str, int]]:
    """Which subreddits the day's board actually needs, most wanted first.

    Ranked by how many of today's picks are currently unreadable, so a pass that
    can only afford a few reads spends them where the founder is looking.
    """
    try:
        import shortlist
        picks = shortlist.build(con, want=20)["picks"]
    except Exception as e:
        # No shortlist here. Fall back to the plainest version of the same
        # question: where are the recent unread posts, by subreddit. Worse
        # ordering, same job, and it still never reads a subreddit for nothing.
        print("  bodies: no shortlist (%s), going by recent unread posts" % e)
        picks = [dict(r) for r in con.execute(
            "SELECT subreddit, snippet, post_text FROM posts "
            "WHERE status = 'todo' "
            "AND created_utc > strftime('%s','now') - 168 * 3600")]
    need: dict[str, int] = {}
    for p in picks:
        if len((p.get("post_text") or p.get("snippet") or "").strip()) >= SHORT:
            continue
        sub = (p.get("subreddit") or "").strip()
        if sub:
            need[sub] = need.get(sub, 0) + 1
    return sorted(need.items(), key=lambda kv: -kv[1])[:limit]


def fill(con, entries: list[dict]) -> tuple[int, int]:
    """Write the bodies we just read into any post we already have on file.

    Returns (filled, seen). Only rows whose body is currently shorter than what
    the feed just handed over are touched, so this never shortens a post and
    re-running it is free.
    """
    filled = 0
    for d in entries:
        body = (d.get("body") or "").strip()
        pid = d.get("id") or ""
        if not pid or len(body) < 20:
            continue
        row = con.execute(
            "SELECT COALESCE(post_text, '') t FROM posts WHERE id = ?",
            (pid,)).fetchone()
        if row is None:
            continue                      # a post this machine never collected
        if len(row["t"]) >= len(body):
            continue                      # already have it, or better
        con.execute("UPDATE posts SET post_text = ?, post_text_at = ? WHERE id = ?",
                    (body, store.now(), pid))
        filled += 1
    con.commit()
    return filled, len(entries)


def run(subs: int = SUBS, gap: float = GAP, dry_run: bool = False) -> int:
    con = store.connect()
    targets = wanted_subs(con, subs)
    if not targets:
        print("  bodies: nothing on the shortlist is missing its post")
        con.close()
        return 0
    print("  bodies: %d subreddit%s to read, %s"
          % (len(targets), "" if len(targets) == 1 else "s",
             ", ".join("r/%s (%d)" % (s, n) for s, n in targets)))
    if dry_run:
        con.close()
        return 0

    started = time.time()
    total, read = 0, 0
    for i, (sub, _want) in enumerate(targets):
        if (time.time() - started) / 60 >= MAX_MINUTES:
            print("  bodies: stopping at %.0f minutes, %d subreddit%s unread"
                  % (MAX_MINUTES, len(targets) - i,
                     "" if len(targets) - i == 1 else "s"))
            break
        if i:
            time.sleep(gap)
        # One retry only. A second 429 means the budget is genuinely spent for
        # now, and the next hourly pass is a better place to spend a minute than
        # this one.
        for attempt in (1, 2):
            try:
                entries = read_feed(sub)
                got, seen = fill(con, entries)
                total += got
                read += 1
                print("    r/%-24s %3d posts in the feed, %3d bodies filled"
                      % (sub, seen, got))
                break
            except Throttled:
                if attempt == 1:
                    time.sleep(gap)
                    continue
                print("    r/%-24s throttled twice, left for the next pass" % sub)
            except Exception as e:
                print("    r/%-24s %s: %s" % (sub, type(e).__name__, str(e)[:70]))
                break
    con.close()
    print("  bodies: filled %d post%s from %d feed%s in %.0fs"
          % (total, "" if total == 1 else "s", read,
             "" if read == 1 else "s", time.time() - started))
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subs", type=int, default=SUBS,
                    help="how many subreddit feeds to read, one per minute")
    ap.add_argument("--gap", type=float, default=GAP,
                    help="seconds between feeds (the measured limit is 60)")
    ap.add_argument("--dry-run", action="store_true",
                    help="say which subreddits it would read, and stop")
    a = ap.parse_args()
    run(max(1, a.subs), max(5.0, a.gap), a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
