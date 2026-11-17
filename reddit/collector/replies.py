#!/usr/bin/env python3
"""Who replied to you, pulled from Reddit's private "comment replies" RSS feed.

    python3 scratch/reddit/replies.py            # fetch and file new replies
    python3 scratch/reddit/replies.py --list     # show what is on file
    python3 scratch/reddit/replies.py --file X   # parse a saved feed, for testing

Why this feed and not scraping: Reddit login-walled logged-out reads in August
2026, and the per-thread `.rss` that still answers rate-limits to roughly one
request before returning 429. Polling a dozen threads would be a dozen throttled
requests that still could not tell a reply to YOU from any other new comment.
This is one request, already filtered to replies addressed to you, and it needs
no API app registration.

The feed URL is a bearer credential: anyone holding it can read your replies. It
lives in `reddit_creds.json`, which is gitignored, and is never logged. Set it
with `bash scratch/reddit/set-inbox-feed.sh`, which reads it from the clipboard.

Changing your Reddit password invalidates the feed. That shows up here as a 403
or an empty feed, and re-running the setup script fixes it.
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import pathlib
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125 Safari/537.36")

sys.path.insert(0, str(HERE))
import store  # noqa: E402

# Beside the store, never beside this file. The feed URL is a bearer credential
# and this module is version controlled now, so it is asked for the same way
# `monitor.py` asks: `store._data_dir` works out that the data still lives in
# `scratch/reddit`, from its own position. Reading it from this file's directory
# instead would find nothing in the repo, and a missing feed looks exactly like
# an inbox with nothing in it.
CREDS = store.DATA / "reddit_creds.json"


SCHEMA = """
CREATE TABLE IF NOT EXISTS replies (
  id          TEXT PRIMARY KEY,   -- reddit's comment id, so a re-fetch is a no-op
  post_id     TEXT,               -- t3_... joins to posts.id when we collected it
  subreddit   TEXT,
  author      TEXT,
  body        TEXT,
  url         TEXT,
  created_at  TEXT,
  first_seen  TEXT,
  acknowledged INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS replies_post ON replies(post_id);
CREATE INDEX IF NOT EXISTS replies_new  ON replies(acknowledged, created_at);
"""

# /r/<sub>/comments/<thread>/<slug>/<comment>/
PERMALINK = re.compile(
    r"/r/(?P<sub>[^/]+)/comments/(?P<thread>[a-z0-9]+)/[^/]*/?(?P<comment>[a-z0-9]+)?/?\s*$",
    re.I)


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # Which feed it arrived on, so "someone answered your comment" and "someone
    # commented on your post" can be told apart on the board. Added after the
    # table existed, hence the check rather than a column in SCHEMA.
    cols = {r[1] for r in con.execute("PRAGMA table_info(replies)")}
    if "kind" not in cols:
        con.execute("ALTER TABLE replies ADD COLUMN kind TEXT DEFAULT 'comments'")
    con.commit()


# The two feeds worth having, and what each one catches. "comments" is replies
# to comments you left on other people's threads, which is where the recruiting
# actually happens; "selfreply" is comments on threads you posted yourself.
# Reddit's inbox is split into several RSS sections and any of them can be set.
# Friendly names for the ones worth having; anything else Reddit offers is still
# accepted and just carries its own section name, so this does not have to be
# kept in step with whatever that page is called this year.
FEED_LABELS = {
    "comments": "replies to your comments",
    "selfreply": "replies to your posts",
    "mentions": "mentions of your username",
    "messages": "private messages",
    "unread": "anything unread",
    "inbox": "the whole inbox",
}

# Fetch order, most specific first. `unread` and `inbox` overlap the others, so
# if one of those is read first every reply lands tagged "unread" and you lose
# the useful distinction. Filing is keyed on the entry id and skips what it
# already has, so whichever feed gets there first decides the label. Specific
# feeds therefore go first on purpose.
FEED_ORDER = ["comments", "selfreply", "mentions", "messages", "unread", "inbox"]


def feed_urls() -> list:
    """Every replies feed that has been set, as (kind, url).

    Reads the newer `inbox_feeds` map and falls back to the single `inbox_rss`
    that earlier setups wrote, so a machine that has only ever been given the
    one link keeps working untouched.
    """
    if not CREDS.exists():
        return []
    try:
        creds = json.load(open(CREDS)) or {}
    except Exception:
        return []
    feeds = creds.get("inbox_feeds") or {}
    out = [(k, u) for k, u in feeds.items() if u]
    if not any(k == "comments" for k, _ in out) and creds.get("inbox_rss"):
        out.append(("comments", creds["inbox_rss"]))
    # Most specific first. Anything Reddit offers that is not in FEED_ORDER
    # sorts after the known ones rather than being dropped.
    return sorted(out, key=lambda kv: (FEED_ORDER.index(kv[0])
                                       if kv[0] in FEED_ORDER else len(FEED_ORDER),
                                       kv[0]))


def feed_url() -> str | None:
    """Kept for callers that only ever wanted the comment-replies feed."""
    for kind, url in feed_urls():
        if kind == "comments":
            return url
    return None


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    return m.group(1).strip() if m else ""


def _text(raw: str) -> str:
    """Feed content is escaped HTML. Unescape, strip tags, collapse space."""
    s = html.unescape(raw)
    s = re.sub(r"<br\s*/?>|</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", html.unescape(s)).strip()


# A private message has no thread and no subreddit, so it cannot match
# PERMALINK. Its link is /message/messages/<id>, and that id is what makes it
# stable to key on.
MESSAGE_LINK = re.compile(r"/message/[^/]+/(?P<id>[a-z0-9]+)/?\s*$", re.I)


def parse_feed(xml: str) -> list[dict]:
    """Reddit's Atom feed, one dict per entry.

    Handles both shapes the inbox produces. A comment reply, a post reply and a
    mention all carry a thread permalink and parse the same way. A private
    message does not: it has no subreddit and no thread, and an earlier version
    of this silently dropped every one of them because the permalink pattern
    could not match.
    """
    out = []
    for block in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        link = re.search(r'<link[^>]*href="([^"]+)"', block)
        if not link:
            continue
        url = html.unescape(link.group(1))
        path = urllib.parse.urlparse(url).path
        author = _tag(block, "name").lstrip("/").removeprefix("u/")
        common = {
            "author": author,
            "body": _text(_tag(block, "content"))[:2000],
            "url": url,
            "created_at": _tag(block, "updated") or _tag(block, "published"),
        }

        m = PERMALINK.search(path)
        if m and m.group("comment"):
            out.append({**common, "id": m.group("comment"),
                        "post_id": "t3_" + m.group("thread"),
                        "subreddit": m.group("sub")})
            continue

        dm = MESSAGE_LINK.search(path)
        if dm:
            # No thread to join to, and deliberately no fake subreddit: the
            # board shows a blank rather than inventing r/something.
            out.append({**common, "id": dm.group("id"),
                        "post_id": None, "subreddit": None})
            continue
        # Anything else is a shape we do not recognise. Skipped rather than
        # guessed at, because a row with no stable id would be re-filed as new
        # on every single fetch.
    return out


def fetch(url: str, timeout: int = 30) -> str:
    """One feed, with patience for the throttle.

    Reddit rate limits these feeds per IP, and hard: reading several sections
    back to back gets the first and 429s the rest. Without the retry below, the
    run after this became multi-feed would have fetched comment replies and
    failed every other section, every hour, printing a 429 and moving on. The
    throttle clears in seconds, so waiting it out is the whole fix.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for pause in (0, 2.0, 5.0, 10.0):
        if pause:
            time.sleep(pause)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                raise RuntimeError(
                    "Reddit rejected the replies feed. The usual cause is a "
                    "password change, which invalidates it. Re-run "
                    "scratch/reddit/set-inbox-feed.sh") from e
            last = e
            if e.code == 429:
                continue              # throttled, wait longer and try again
            raise RuntimeError(f"replies feed: HTTP {e.code}") from e
        except Exception as e:        # timeouts and connection resets
            last = e
            continue
    raise RuntimeError(
        f"replies feed: gave up after retrying ({last}). Reddit throttles these "
        f"per IP; the next hourly run will try again.")


# The account had a life before this project. Reddit's own feeds serve that life
# back forever, so three replies from December 2020 about Kanye and Rap Genius
# sat in the Replies list next to the three that actually matter, which is half
# the list being noise. Nothing before this floor is this product's business.
EARLIEST = "2026-07-01"


def file_replies(con: sqlite3.Connection, rows: list[dict],
                 acknowledged: int = 0) -> dict:
    """Store new replies. Filing the same reply twice is a no-op by design.

    `acknowledged=1` files them as already dealt with, which is how the backlog
    sitting in the feed on the day you set it up is absorbed without any of it
    showing up as something to answer. Deleting those rows would NOT achieve
    that: the next fetch would see the same entries, find nothing on file, and
    present them all as new. Being on file as seen is what makes them stay quiet.

    Anything older than `EARLIEST` is skipped outright, which is different from
    being filed as acknowledged: these are not a backlog to absorb, they are
    another era of the account and they should never appear at all.
    """
    ensure_schema(con)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    fresh, old = 0, 0
    for r in rows:
        if (r.get("created_at") or "") and r["created_at"][:10] < EARLIEST:
            old += 1
            continue
        cur = con.execute("SELECT 1 FROM replies WHERE id = ?", (r["id"],)).fetchone()
        if cur:
            continue
        con.execute(
            "INSERT INTO replies (id, post_id, subreddit, author, body, url, "
            "created_at, first_seen, kind, acknowledged) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r["post_id"], r["subreddit"], r["author"], r["body"],
             r["url"], r["created_at"], now, r.get("kind") or "comments",
             acknowledged))
        fresh += 1
        # Someone who answered you is the warmest contact this system can find,
        # which is the whole point of tracking this. File them next to the
        # commenters we already keep rather than inventing a second list.
        if acknowledged:
            continue          # baselining history, not finding someone to answer
        try:
            con.execute(
                "INSERT OR IGNORE INTO people (author, post_id, subreddit, kind, "
                "quote, url, first_at, saved, note) VALUES (?,?,?,?,?,?,?,0,?)",
                (r["author"], r["post_id"], r["subreddit"], "replied-to-you",
                 r["body"][:500], r["url"], now,
                 "commented on your post" if r.get("kind") == "selfreply"
                 else "replied to your comment"))
        except sqlite3.Error:
            pass          # people is a convenience here, never a reason to fail
    con.commit()
    return {"seen": len(rows), "new": fresh, "too_old": old}


def unacknowledged(con: sqlite3.Connection) -> list[dict]:
    ensure_schema(con)
    cur = con.execute(
        "SELECT * FROM replies WHERE acknowledged = 0 ORDER BY created_at DESC")
    return [dict(r) for r in cur.fetchall()]


def run(acknowledged: int = 0) -> int:
    feeds = feed_urls()
    if not feeds:
        print("  no replies feed set. Copy the 'comment replies only' and "
              "'post replies only' RSS links from reddit.com/prefs/feeds, then "
              "run scratch/reddit/set-inbox-feed.sh once for each")
        return 0

    con = store.connect()
    total_new = 0
    # Between feeds, not just inside fetch's retry: starting each section a few
    # seconds apart means the first attempt usually succeeds rather than always
    # burning a retry.
    for i, (kind, url) in enumerate(feeds):
        if i:
            time.sleep(3.0)
        label = FEED_LABELS.get(kind, kind)
        try:
            xml = fetch(url)
        except RuntimeError as e:
            # One dead feed must not stop the other. A password change kills
            # both, but a single bad link should only cost its own half.
            print(f"  ! {label}: {e}")
            continue
        rows = parse_feed(xml)
        for r in rows:
            r["kind"] = kind
        res = file_replies(con, rows, acknowledged=acknowledged)
        total_new += res["new"]
        print(f"  {label}: {res['new']} new, {res['seen']} in the feed")
    missing = [FEED_LABELS[k] for k in FEED_LABELS
               if k not in {kind for kind, _ in feeds}]
    total = con.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
    con.close()
    print(f"  replies on file: {total}")
    if missing:
        print(f"  (not watching {missing[0]} yet, run set-inbox-feed.sh with "
              f"that link)")
    return total_new


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show replies on file")
    ap.add_argument("--seed", action="store_true",
                    help="file everything currently in the feeds as ALREADY SEEN, "
                         "so the backlog never shows up as something to answer. "
                         "Run this once, right after setting the feeds up.")
    ap.add_argument("--file", help="parse a saved feed instead of fetching")
    a = ap.parse_args()

    if a.file:
        rows = parse_feed(pathlib.Path(a.file).read_text())
        print(f"parsed {len(rows)} replies")
        for r in rows:
            print(f"  u/{r['author']} in r/{r['subreddit']} on {r['post_id']}")
            print(f"    {r['body'][:100]}")
            print(f"    {r['url']}")
        return 0

    if a.list:
        con = store.connect()
        ensure_schema(con)
        rows = con.execute(
            "SELECT r.*, p.title FROM replies r LEFT JOIN posts p ON p.id = r.post_id "
            "ORDER BY r.created_at DESC").fetchall()
        con.close()
        if not rows:
            print("no replies on file yet")
            return 0
        for r in rows:
            tag = "your post" if (r["kind"] or "") == "selfreply" else "your comment"
            print(f"u/{r['author']}  r/{r['subreddit']}  {(r['created_at'] or '')[:16]}  "
                  f"(on {tag})")
            if r["title"]:
                print(f"  on: {r['title'][:70]}")
            print(f"  {(r['body'] or '')[:140]}")
            print(f"  {r['url']}\n")
        return 0

    if a.seed:
        if not feed_urls():
            # Saying "baselined 0 entries" here would read as success when in
            # fact nothing is set up yet and the order has been got wrong.
            print("  nothing to baseline: no feeds are set yet. Run "
                  "scratch/reddit/set-inbox-feed.sh for each link FIRST, then "
                  "come back and run this once.")
            return 1
        con = store.connect()
        before = len(unacknowledged(con))
        n = run(acknowledged=1)
        # Also settle anything ALREADY on file. Filing skips ids it has seen, so
        # without this a baseline would quietly leave whatever an earlier run had
        # fetched still sitting there as things to answer, which is the opposite
        # of what asking for a baseline means.
        con = store.connect()
        settled = con.execute(
            "UPDATE replies SET acknowledged = 1 WHERE acknowledged = 0").rowcount
        con.commit()
        con.close()
        print()
        if settled:
            print(f"  also settled {settled} already on file from an earlier run")
        print(f"  baselined. {n} entries are now on file as already seen, so they")
        print("  will never appear as something to answer. From here only genuinely")
        print("  new replies count. Unanswered before this: " + str(before))
        return 0

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
