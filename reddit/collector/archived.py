#!/usr/bin/env python3
"""Tag a queued Reddit thread as archived or commentable, when that can be told.

Reddit locks old threads: you cannot comment on an archived post. Serving those
in the reply queue wastes the founder's time, so a thread can be split:
  - commentable  -> the reply queue (act on these)
  - archived     -> the voice lab (can't reply; study them to shape our voice
                    and mine ICP signal)

Detection needs no Reddit API. Logged-out old.reddit HTML marks an archived
post two ways, and we accept either:
  1. the vote arrows carry an `archived` class (fresh threads do not),
  2. the "new comments cannot be posted" archived infobar text.
Verified 2026-07-17: a fresh r/socialskills thread lacks the arrow class; the
AEO-cited seed threads carry it.

WHAT CHANGED, measured here on 2026-10-18
-----------------------------------------
Logged-out per-thread reads are gone. Every one of the 21 queued threads, and a
thread id that does not exist, answered the same way: HTTP 200, 333KB, redirected
to old.reddit.com/login/?reason=lor2, titled "Welcome to Reddit". See the same
measurement from the other direction in `reddit/WHERE-IT-RUNS.md`.

A login page does not raise, and it carries neither archived marker, so the old
code returned False for it and the board read that as "you can reply to this".
It said "commentable" about a post that does not exist. That is why the fetch
below has to recognise the page it got, and why it returns None, not False, for
anything that is not a thread: not knowing is an answer, and a wrong "you can
reply" is not.

The cockpit's Refresh button no longer calls this. 21 requests that can only
answer "walled" are not worth a minute of the founder's Refresh, and the field
only ever reached one panel (`compute_content_plan`), never the board's own reply
queue, which has been built from the store rather than from queue.json for a
while now. That route was taken on the same day: the OAuth listing the collector
already reads carries `archived` and `locked` on every post, and `monitor._from_api`
now keeps both into `posts`, where the board and the phone can read them. So the
split is back, from a source that costs nothing, and this file is not in it.

So this is a hand tool now. It still works, and it will tag a queue again by
itself the day a thread read answers with a thread.

    python3 archived.py <url>      # print archived status for one url
    python3 archived.py            # tag any untagged threads in queue.json
    python3 archived.py --refresh  # re-check every thread
    python3 archived.py --selftest # check the rule itself, no network
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store  # noqa: E402

# This file is version controlled now, so the cockpit's Refresh button is not one
# copy on one laptop. queue.json is not and must not be: it is a list of real
# people's threads, and reddit_creds.json sits beside it. It used to be "next to
# this script", which was the same place until the script moved. store.DATA is
# the one answer to where the data lives, the same one monitor.py writes the
# queue with, so the two cannot drift.
QUEUE = store.DATA / "queue.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125 Safari/537.36")

ARROW_ARCHIVED = re.compile(r'class="arrow (?:up|down)[^"]*\barchived\b')
INFOBAR_ARCHIVED = re.compile(r"new comments cannot be posted", re.I)

# The login wall, by the two signals it actually shows: the page Reddit redirects
# to, and that page's title. `enrich.scrape_thread` already refuses this page by
# the same pair, from the days when the fallback below still returned real HTML,
# so this is the engine's existing rule rather than a new guess.
# Anchored at the PATH, not anywhere in the URL, because r/login exists and a
# thread in it would otherwise read as the wall and stop a whole pass.
LOGIN_PATH = re.compile(r"^https?://[^/]+/login\b", re.I)
LOGIN_TITLE = re.compile(r"welcome to reddit", re.I)

# Positive proof the page IS a comments page, so an interstitial, an error page
# or a removed post cannot be read as a fresh thread either. Any one of these is
# enough. They are not invented: the arrows are the markup ARROW_ARCHIVED reads
# the archived class out of, and `class="md"` is the post body `enrich` parses,
# so a real thread page that carried none of them could not have been detected
# as archived in the first place. The login page carries none of them (measured
# 2026-10-18, all four absent from all 333KB).
THREAD_MARKERS = ('class="arrow ', 'class="md"', 'commentarea', 'id-t3_')

WALLED = "login wall"


def to_old(url: str) -> str:
    old = url.replace("://www.", "://old.").replace("://reddit.", "://old.reddit.")
    if "old.reddit.com" not in old:
        old = old.replace("://reddit.com", "://old.reddit.com")
    return old


def fetch_thread(url: str) -> tuple[str | None, str]:
    """The thread's old.reddit HTML, or (None, why it is not a thread page).

    Separated from the verdict because the CALLER needs to know the difference
    between "this one fetch failed" (try again later) and WALLED (this machine
    cannot read any thread, so stop asking).
    """
    try:
        req = urllib.request.Request(to_old(url), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = resp.read().decode("utf-8", errors="ignore")
            final = resp.geturl() or ""
    except Exception as e:
        return None, f"fetch failed ({type(e).__name__})"
    if LOGIN_PATH.match(final) or LOGIN_TITLE.search(page[:4000]):
        return None, WALLED
    if not any(m in page for m in THREAD_MARKERS):
        return None, "not a thread page"
    return page, ""


def check(url: str) -> tuple[bool | None, str]:
    """(True archived, False commentable, None don't know), and why if None."""
    page, why = fetch_thread(url)
    if page is None:
        return None, why
    return bool(ARROW_ARCHIVED.search(page) or INFOBAR_ARCHIVED.search(page)), ""


def is_archived(url: str) -> bool | None:
    """True/False, or None if we couldn't tell."""
    return check(url)[0]


def wall_note() -> str:
    return (
        "\nold.reddit answered its login page, not a thread, so nothing here can\n"
        "be told apart and nothing was written. This is the machine, not the\n"
        "thread: logged-out per-thread reads have been dead everywhere since\n"
        "2026-10-17 (see reddit/WHERE-IT-RUNS.md). Untagged threads stay in the\n"
        "reply queue, which is the right default for posts collected from /new.\n"
        "The route to a real answer is the OAuth listing's own `archived` and\n"
        "`locked`, in monitor._from_api, not this file.")


def tag_queue(refresh: bool = False) -> None:
    if not QUEUE.exists():
        print("no queue.json to tag")
        return
    data = json.loads(QUEUE.read_text())
    threads = data.get("threads", [])
    # `is None` rather than "not in", so a thread left unknown by an earlier pass
    # is retried instead of being stuck unknown until someone passes --refresh.
    todo = [t for t in threads if refresh or t.get("archived") is None]
    print(f"{len(threads)} threads, checking {len(todo)}...")
    learned = unknown = 0
    walled = False
    for i, t in enumerate(todo, 1):
        res, why = check(t.get("url", ""))
        if res is None and why == WALLED:
            walled = True
            break
        if res is None:
            # Left absent on purpose. Writing a null would say "we checked and
            # could not tell", which reads the same as "we know nothing" to the
            # board but stops the next pass from ever retrying it.
            unknown += 1
            tag = f"unknown ({why})"
        else:
            t["archived"] = res
            learned += 1
            tag = "archived" if res else "commentable"
        print(f"  [{i}/{len(todo)}] {tag:28} {t.get('title','')[:50]}")
        if i < len(todo):
            time.sleep(1)
    if walled:
        print(wall_note())
    if learned:
        QUEUE.write_text(json.dumps(data, indent=2))
    # totals across the whole queue (not just this pass)
    a = sum(1 for t in threads if t.get("archived") is True)
    c = sum(1 for t in threads if t.get("archived") is False)
    print(f"\ndone, {learned} learned this pass"
          + (f", {unknown} still unknown" if unknown else "")
          + (" (queue.json untouched)" if not learned else "")
          + f". queue now: {c} commentable, {a} archived (voice lab), "
          + f"{len(threads)-a-c} unknown.")


# --------------------------------------------------------------- the self-test
# There is no network in this. It exists because the rule above cannot be
# checked against a live thread any more: this machine gets the login page for
# every thread, so a rule that called every real thread "unknown" would look
# exactly as healthy as a correct one. These pages carry the markup the two
# archived markers read, so a change that breaks the verdict on a real thread
# fails here instead of silently emptying the reply queue.
#
#     python3 archived.py --selftest
_ARCHIVED_PAGE = """<html><head><title>my friends drifted : socialskills</title>
</head><body><div class="content"><div id="siteTable">
<div class=" thing id-t3_abc123 link"><div class="midcol unvoted">
<div class="arrow up archived"></div><div class="arrow down archived"></div></div>
<div class="entry"><a class="title">my friends drifted</a>
<div class="usertext-body"><div class="md"><p>the post body</p></div></div>
</div></div></div><div class="commentarea"><div class="infobar">
This thread is archived<br>New comments cannot be posted and votes cannot be cast
</div></div></div></body></html>"""

_FRESH_PAGE = """<html><head><title>my friends drifted : socialskills</title>
</head><body><div class="content"><div id="siteTable">
<div class=" thing id-t3_abc123 link"><div class="midcol unvoted">
<div class="arrow up login-required"></div>
<div class="arrow down login-required"></div></div>
<div class="entry"><a class="title">my friends drifted</a>
<div class="usertext-body"><div class="md"><p>the post body</p></div></div>
</div></div></div><div class="commentarea">
<form class="usertext"><textarea></textarea></form></div></div></body></html>"""

# Archived by the infobar alone, with unmarked arrows: the second accepted marker.
_INFOBAR_PAGE = _FRESH_PAGE.replace(
    '<form class="usertext"><textarea></textarea></form>',
    '<div class="infobar">new comments cannot be posted</div>')

_LOGIN_PAGE = """<html><head><title>Welcome to Reddit</title></head><body>
<div id="app"><h1>Welcome to Reddit</h1><p>Dive into anything</p>
<button>Log In</button></div></body></html>"""

_NOT_FOUND_PAGE = """<html><head><title>reddit.com: page not found</title></head>
<body><div class="content"><h1>page not found</h1></div></body></html>"""

_OVER18_PAGE = """<html><head><title>over 18?</title></head><body>
<div class="content"><p>You must be 18+ to view this community</p>
<form action="/over18" method="post"><button>Continue</button></form>
</div></body></html>"""

_CASES = [
    ("an archived thread", _ARCHIVED_PAGE, "", True),
    ("a fresh thread", _FRESH_PAGE, "", False),
    ("archived by the infobar alone", _INFOBAR_PAGE, "", True),
    ("the login page", _LOGIN_PAGE, "", None),
    ("a thread url redirected to /login", _FRESH_PAGE,
     "https://old.reddit.com/login/?reason=lor2&dest=x", None),
    ("a thread IN r/login, which is not the wall", _FRESH_PAGE,
     "https://old.reddit.com/r/login/comments/abc123/t/", False),
    ("page not found", _NOT_FOUND_PAGE, "", None),
    ("the over-18 interstitial", _OVER18_PAGE, "", None),
    ("an empty body", "", "", None),
]


class _Resp:
    def __init__(self, page, final):
        self._page, self._final = page, final

    def read(self):
        return self._page.encode()

    def geturl(self):
        return self._final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def selftest() -> int:
    url = "https://www.reddit.com/r/socialskills/comments/abc123/t/"
    real = urllib.request.urlopen
    bad = 0
    try:
        for name, page, final, want in _CASES:
            urllib.request.urlopen = (
                lambda req, timeout=30, _p=page, _f=final: _Resp(_p, _f or to_old(url)))
            got, why = check(url)
            bad += got is not want
            print(f"  {'ok  ' if got is want else 'FAIL'} {name:36} "
                  f"-> {str(got):5} {why}")

        def raises(req, timeout=30):
            raise TimeoutError("timed out")

        urllib.request.urlopen = raises
        got, why = check(url)
        good = got is None and why != WALLED
        bad += not good
        # A fetch that raises must NOT read as the wall, or one slow thread
        # would stop a whole pass that could have carried on.
        print(f"  {'ok  ' if good else 'FAIL'} {'a fetch that raises':36} "
              f"-> {got} {why}")
    finally:
        urllib.request.urlopen = real
    print("\nall pass" if not bad else f"\n{bad} FAILED")
    return 0 if not bad else 1


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if args and args[0].startswith("http"):
        res, why = check(args[0])
        if res is None:
            print(f"unknown ({why})")
            if why == WALLED:
                print(wall_note())
        else:
            print("archived" if res else "commentable")
        return 0
    tag_queue(refresh="--refresh" in args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
