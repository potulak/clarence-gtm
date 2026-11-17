#!/usr/bin/env python3
"""Clarence Reddit listening loop: find fresh, high-fit threads to answer.

Two ways in, from the widest net to the narrowest:

  1. SITEWIDE SEARCH on old.reddit.com for each theme in watch.json. This is the
     one that finds you posts to answer: it reads the post BODY and it looks
     across all of Reddit, so threads in subs you never thought to watch show up.
  2. The /new listing of each watched sub, which carries exact reply counts.
  3. Reddit's OAuth API, if you ever fill in reddit_creds.json. Not needed.

Everything is matched against the watch.json themes and ranked by
fit x freshness x opportunity. Writes queue.json, which the cockpit UI reads.

    python3 scratch/reddit/monitor.py            # refresh the queue

No app, no key, no login. Reddit answers 403 to the unauthenticated JSON API and
429s the .rss feeds after about one request, but old.reddit.com's HTML answers
fine, so that is what this reads. It paces itself and backs off when asked.

Presence, not promotion: this tool only FINDS conversations. You read, you draft
in your own voice, you post manually in your own browser. It never posts.

Stdlib only. Paces requests to stay polite to Reddit.
"""
from __future__ import annotations

import base64
import datetime
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store

HERE = Path(__file__).resolve().parent

# The watchlist is configuration, so it travels WITH this file and is version
# controlled alongside it. Everything else here is data or secret, so it stays
# in store.DATA, outside the repo. See store._data_dir for why that split is
# worked out rather than configured.
WATCH = Path(os.environ.get("CLARENCE_REDDIT_WATCH") or HERE / "watch.json")
QUEUE = store.DATA / "queue.json"
CREDS = store.DATA / "reddit_creds.json"


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for p in ("/etc/ssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem",
              "/usr/local/etc/openssl@3/cert.pem"):
        if os.path.exists(p):
            try:
                ctx.load_verify_locations(p)
                break
            except Exception:
                pass
    return ctx


SSL_CTX = _ssl_ctx()


_REPO = HERE.parents[1] if HERE.name == "collector" else HERE.parent
AEO_ENV = Path.home() / "Library" / "Application Support" / "clarence-aeo" / ".env"


def load_env() -> None:
    """Pick up ANTHROPIC_API_KEY the same way the AEO + enrich tools do."""
    for path in (AEO_ENV, _REPO / "aeo" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


DEFAULT_LANE = "conversations"


def load_watch(lane: str = DEFAULT_LANE) -> dict:
    """One lane's config. Falls back to the old flat file shape if it predates lanes."""
    w = json.loads(WATCH.read_text())
    lanes = w.get("lanes")
    if not lanes:
        return w
    cfg = dict(lanes.get(lane) or lanes[DEFAULT_LANE])
    cfg["username"] = w.get("username", "clarence")
    cfg["lane"] = lane
    return cfg


def all_lanes() -> list[str]:
    w = json.loads(WATCH.read_text())
    return list((w.get("lanes") or {DEFAULT_LANE: {}}).keys())


def load_creds() -> dict | None:
    """The Reddit script app, from the environment first and the file second.

    The environment comes first because that is how a runner gets them: as
    repository secrets, which are never written to disk and never land in a log.
    The file is the laptop's way, and it is mode 600 and gitignored.
    """
    cid = (os.environ.get("REDDIT_CLIENT_ID") or "").strip()
    sec = (os.environ.get("REDDIT_CLIENT_SECRET") or "").strip()
    if cid and sec:
        return {"client_id": cid, "client_secret": sec}
    if CREDS.exists():
        try:
            c = json.loads(CREDS.read_text())
            if c.get("client_id") and c.get("client_secret"):
                return c
        except Exception:
            pass
    return None


def get_token(creds: dict, ua: str) -> str | None:
    """App-only OAuth (client_credentials). Read-only, no user password."""
    auth = base64.b64encode(
        f"{creds['client_id']}:{creds['client_secret']}".encode()
    ).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=body,
        headers={"Authorization": f"Basic {auth}", "User-Agent": ua},
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            return json.loads(r.read()).get("access_token")
    except Exception as e:
        print(f"  ! token request failed: {e}")
        return None


def get_json(url: str, ua: str, token: str, tries: int = 3) -> dict | None:
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": ua, "Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            if getattr(e, "code", None) == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if attempt == tries - 1:
                print(f"  ! {url.split('?')[0]}: {e}")
            time.sleep(2)
    return None


def posts_from(listing: dict | None) -> list[dict]:
    if not listing:
        return []
    return [c.get("data", {}) for c in listing.get("data", {}).get("children", [])]


def _has_word(low: str, word: str) -> bool:
    """Whole-word match so 'personal' does not hit 'personality'."""
    return re.search(r"\b" + re.escape(word) + r"\b", low) is not None


def match(text: str, clusters: dict[str, list[str]]):
    low = text.lower()
    best = (0.0, "", "")
    for cluster, phrases in clusters.items():
        for ph in phrases:
            p = ph.lower()
            if p in low:                       # whole phrase present = strong
                score = 1.0
            else:
                # A theme match needs at least TWO distinctive words (len > 3),
                # each present as a whole word. This stops a single common word
                # like "personal" (from "personal crm") matching "personality"
                # and flooding the queue with off-topic posts.
                toks = [t for t in p.split() if len(t) > 3]
                if len(toks) >= 2 and all(_has_word(low, t) for t in toks):
                    score = 0.55
                else:
                    continue
            if score > best[0]:
                best = (score, cluster, ph)
    return best


def score_thread(match_score: float, age_hours: float, num_comments: int) -> float:
    fresh = max(0.0, 1.0 - age_hours / 72.0)
    opp = 1.0 if num_comments <= 8 else 0.5 if num_comments <= 25 else 0.2
    return round(0.5 * match_score + 0.3 * fresh + 0.2 * opp, 3)


def _why(cluster: str, phrase: str, m: float, age_h: float, ncom: int) -> str:
    hit = "exact match" if m >= 1.0 else "theme match"
    fresh = f"{age_h:.0f}h old" if age_h < 48 else f"{age_h/24:.0f}d old"
    room = "few replies (room for a top answer)" if ncom <= 8 else f"{ncom} replies"
    return f"{cluster}: {hit} on “{phrase}” · {fresh} · {room}"


def collect(token: str, ua: str) -> list[dict]:
    w = load_watch()
    clusters = w["clusters"]
    now = time.time()
    seen: dict[str, dict] = {}

    sources = [f"https://oauth.reddit.com/r/{urllib.parse.quote(s)}/new?limit=40"
               for s in w["subreddits"]]
    for phrases in clusters.values():
        q = urllib.parse.quote(phrases[0])
        sources.append(f"https://oauth.reddit.com/search?q={q}&sort=new&limit=25&type=link")

    for i, url in enumerate(sources):
        for d in posts_from(get_json(url, ua, token)):
            pid = d.get("name") or d.get("id")
            if not pid or pid in seen:
                continue
            title, body = d.get("title", "") or "", d.get("selftext", "") or ""
            m_score, cluster, phrase = match(f"{title}\n{body}", clusters)
            if m_score == 0.0:
                continue
            age_h = round((now - d.get("created_utc", now)) / 3600, 1)
            ncom = int(d.get("num_comments", 0) or 0)
            fit = score_thread(m_score, age_h, ncom)
            if fit < 0.3:
                continue
            seen[pid] = {
                "id": pid, "source": "live",
                "subreddit": d.get("subreddit", ""),
                "title": title,
                "url": "https://www.reddit.com" + (d.get("permalink") or ""),
                "age_hours": age_h, "num_comments": ncom,
                "score": int(d.get("score", 0) or 0),
                "matched_cluster": cluster, "matched_phrase": phrase,
                "fit": fit, "why": _why(cluster, phrase, m_score, age_h, ncom),
                "snippet": (body[:280] + "…") if len(body) > 280 else body,
            }
        time.sleep(1)
        print(f"  [{i+1}/{len(sources)}] {len(seen)} candidates so far")

    return sorted(seen.values(), key=lambda t: -t["fit"])[:50]


# ---------------------------------------------------------------- RSS path
# No Reddit app needed. Reddit exposes each sub's newest posts as a public Atom
# feed (old.reddit.com/r/<sub>/new/.rss), readable with no auth. We pull those,
# match against the same clusters, and rank the same way. Search feeds are
# rate-limited hard when unauthenticated, so we stick to the reliable per-sub
# "new" feeds. Polite: real UA, 1s spacing.
RSS_UA = "clarence-gtm-monitor/0.1 (personal project, read-only)"
_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_TAGS = re.compile(r"<[^>]+>")


def _feed_entry(e: str) -> dict:
    import html
    tid = re.search(r"<id>(t3_[a-z0-9]+)</id>", e)
    title = re.search(r"<title>(.*?)</title>", e, re.S)
    link = re.search(r'<link href="([^"]+/comments/[^"]+)"', e)
    pub = re.search(r"<published>(.*?)</published>", e)
    sub = re.search(r'<category term="([^"]+)"', e)
    content = re.search(r"<content[^>]*>(.*?)</content>", e, re.S)
    body = ""
    if content:
        body = html.unescape(_TAGS.sub(" ", html.unescape(content.group(1)))).strip()
    return {
        "id": tid.group(1) if tid else "",
        "title": html.unescape(title.group(1)).strip() if title else "",
        "url": (link.group(1).replace("old.reddit.com", "www.reddit.com")) if link else "",
        "published": pub.group(1) if pub else "",
        "subreddit": sub.group(1) if sub else "",
        "body": body,
    }


def collect_rss(ua: str = RSS_UA) -> list[dict]:
    import datetime
    w = load_watch()
    clusters = w["clusters"]
    now = datetime.datetime.now(datetime.timezone.utc)
    seen: dict[str, dict] = {}
    subs = w["subreddits"]
    for i, sub in enumerate(subs):
        url = f"https://old.reddit.com/r/{urllib.parse.quote(sub)}/new/.rss?limit=50"
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        xml = ""
        for attempt in range(3):                       # unauth RSS is rate-limited per IP
            try:
                with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as r:
                    xml = r.read().decode("utf-8", errors="ignore")
                break
            except urllib.error.HTTPError as ex:
                if ex.code == 429 and attempt < 2:
                    time.sleep(6 * (attempt + 1))
                    continue
                print(f"  [{i+1}/{len(subs)}] r/{sub}: fetch failed ({ex})")
            except Exception as ex:
                print(f"  [{i+1}/{len(subs)}] r/{sub}: fetch failed ({ex})")
                break
        if not xml:
            time.sleep(2)
            continue
        for block in _ENTRY.findall(xml):
            d = _feed_entry(block)
            pid = d["id"]
            if not pid or pid in seen or not d["url"]:
                continue
            m_score, cluster, phrase = match(f"{d['title']}\n{d['body']}", clusters)
            if m_score == 0.0:
                continue
            try:
                ts = datetime.datetime.fromisoformat(d["published"])
                age_h = round((now - ts).total_seconds() / 3600, 1)
            except Exception:
                age_h = 0.0
            fresh = max(0.0, 1.0 - age_h / 72.0)
            fit = round(0.6 * m_score + 0.4 * fresh, 3)   # no comment signal via RSS
            if fit < 0.3:
                continue
            hit = "exact match" if m_score >= 1.0 else "theme match"
            fresh_s = f"{age_h:.0f}h old" if age_h < 48 else f"{age_h/24:.0f}d old"
            seen[pid] = {
                "id": pid, "source": "live-rss", "subreddit": d["subreddit"],
                "title": d["title"], "url": d["url"], "age_hours": age_h,
                "num_comments": None, "score": 0,
                "matched_cluster": cluster, "matched_phrase": phrase, "fit": fit,
            "created_utc": (d["created_ms"] / 1000) if d.get("created_ms") else None,
                "why": f"{cluster}: {hit} on “{phrase}” · {fresh_s}",
                "snippet": (d["body"][:280] + "…") if len(d["body"]) > 280 else d["body"],
            }
        print(f"  [{i+1}/{len(subs)}] r/{sub}: {len(seen)} candidates so far")
        time.sleep(2)
    return sorted(seen.values(), key=lambda t: -t["fit"])[:50]


# ------------------------------------------------------- old.reddit HTML path
# The path that actually works with no Reddit app. Reddit now answers 403 to the
# unauthenticated JSON API, and the .rss feeds on www rate-limit to roughly one
# request before returning 429 for minutes (that is what left the queue empty).
# old.reddit.com's plain HTML answers reliably, so that is what we read.
#
# Two sources, because they cover different ground:
#   1. SITEWIDE SEARCH per cluster. This is the big one: it matches the post
#      BODY and finds threads anywhere on Reddit, not just the watched subs.
#   2. Each watched sub's /new listing, which carries exact reply counts and
#      catches on-topic posts whose wording our phrases would miss in search.
#
# Read-only, paced, cached by nothing, and it never posts.
HTML_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/125 Safari/537.36")
PAUSE = 3.0          # seconds between requests, to stay a polite reader
TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return html.unescape(TAG_RE.sub(" ", html.unescape(s))).strip()


def _attr(block: str, name: str) -> str:
    m = re.search(name + r'="([^"]*)"', block)
    return m.group(1) if m else ""


WALLED = 0          # requests that came back as a login page rather than data


def fetch_html(url: str, tries: int = 3) -> str:
    """GET a page, backing off politely if Reddit asks us to slow down.

    Since 2026-08-31 Reddit answers logged-out reads on old.reddit.com with a 302
    to /login/. urllib follows that, so the naive version of this function handed
    the login page to the parsers, which found no posts in it and reported "filed
    0 posts" as though Reddit had simply been quiet. A whole day was collected as
    zeroes that way. Treat a login page as the failure it is, and say so loudly.
    """
    global WALLED
    req = urllib.request.Request(url, headers={"User-Agent": HTML_UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                body = r.read().decode("utf-8", errors="ignore")
                if "/login/" in r.url or "<title>Welcome to Reddit" in body[:4000]:
                    WALLED += 1
                    print(f"  ! LOGIN WALL, no data: {url.split('?')[0]}")
                    return ""
                return body
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                time.sleep(10 * (attempt + 1))
                continue
            print(f"  ! {url.split('?')[0]}: HTTP {e.code}")
            return ""
        except Exception as e:
            if attempt < tries - 1:
                time.sleep(4)
                continue
            print(f"  ! {url.split('?')[0]}: {e}")
    return ""


def _age_hours_from_iso(stamp: str, now) -> float:
    try:
        ts = datetime.datetime.fromisoformat(stamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return round((now - ts).total_seconds() / 3600, 1)
    except Exception:
        return 0.0


def parse_listing(page: str) -> list[dict]:
    """Posts from a sub's /new page. Title + exact reply count, no body."""
    out = []
    for block in page.split('<div class=" thing ')[1:]:
        pid = _attr(block, "data-fullname")
        if not pid.startswith("t3_") or _attr(block, "data-promoted") == "true":
            continue
        # The title anchor, not the <p class="title"> wrapper: a flaired post
        # puts a flair <span> between the two.
        t = re.search(r'<a[^>]*class="title may-blank[^"]*"[^>]*>(.*?)</a>',
                      block, re.S)
        perma = _attr(block, "data-permalink") or _attr(block, "data-url")
        if not t or "/comments/" not in perma:
            continue
        try:
            ms = int(_attr(block, "data-timestamp") or 0)
        except ValueError:
            ms = 0
        out.append({
            "id": pid,
            "title": _clean(t.group(1)),
            "url": "https://www.reddit.com" + perma,
            "subreddit": _attr(block, "data-subreddit"),
            "author": _attr(block, "data-author"),
            "body": "",
            "created_ms": ms,
            "num_comments": int(_attr(block, "data-comments-count") or 0),
            "score": int(_attr(block, "data-score") or 0),
        })
    return out


def parse_search(page: str) -> list[dict]:
    """Posts from an old.reddit search page. Carries a body excerpt."""
    out = []
    for block in page.split("search-result search-result-link")[1:]:
        pid = _attr(block, "data-fullname")
        if not pid.startswith("t3_"):
            continue
        link = re.search(r'<a href="([^"]*/comments/[^"]+)"[^>]*class="search-title[^"]*"[^>]*>(.*?)</a>',
                         block, re.S)
        if not link:
            continue
        sub = re.search(r'search-subreddit-link[^"]*"[^>]*>r/([^<]+)<', block)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        ncom = re.search(r'search-comments[^"]*"[^>]*>\s*(\d+)', block)
        body = re.search(r'search-result-body[^"]*">(.*?)</div>', block, re.S)
        url = link.group(1).replace("old.reddit.com", "www.reddit.com")
        if url.startswith("/"):
            url = "https://www.reddit.com" + url
        out.append({
            "id": pid,
            "title": _clean(link.group(2)),
            "url": url,
            "subreddit": sub.group(1) if sub else "",
            "author": _attr(block, "data-author"),
            "body": _clean(body.group(1))[:1200] if body else "",
            "created_iso": when.group(1) if when else "",
            "num_comments": int(ncom.group(1)) if ncom else 0,
            "score": 0,
        })
    return out


def _cluster_query(phrases: list[str]) -> str:
    """One search that ORs a cluster's phrases, so one request covers a theme."""
    quoted = [f'"{p}"' for p in phrases[:8]]
    return "(" + " OR ".join(quoted) + ")"


# --------------------------------------------------------------- how it scales
# Watching 80 subreddits does NOT mean 80 requests. old.reddit takes many subs in
# one URL (r/a+b+c), and it takes them on the SEARCH path too, so a whole group
# can be searched for a theme in a single fetch. Verified 2026-08-19: 50 subs in
# one listing URL, 30 subs in one restricted search, a bogus sub name in the list
# is harmless. That is what makes a 25 to 100 sub watchlist cost about 20 fetches.
# Reddit login-walled logged-out reads on old.reddit.com (2026-08-31). www still
# serves the SAME old markup, but only for MULTIREDDIT paths (r/a+b): the modern
# renderer has no multireddit support, so those URLs fall through to the legacy
# one. A single sub on www returns an 8KB JavaScript shell instead. Duplicating
# the name (r/x+x) is what turns one sub back into a multireddit. Verified
# 2026-08-31: 25 posts with live comment counts, scores and timestamps.
LISTING_HOST = "https://www.reddit.com"

CHUNK = 35                # subs per multireddit URL
NATIVE_PAGES = 2          # the floor: pages to read when we have nothing to aim at
SEEN = store.DATA / "seen.json"
SEEN_TTL_DAYS = 30

# How deep a single chunk may ever be walked. The backstop for a first run or a
# long outage, where there is no overlap to stop on and a busy group of subs will
# keep handing over fresh pages indefinitely (measured: 600 unique posts across 6
# pages of one 35-sub group, still going).
MAX_PAGES = 8

# Stop when this much of a page is already on file. That is what "caught up"
# means, and it is the only stopping signal here that can be trusted.
#
# It is deliberately NOT a time floor, which is the obvious design and the wrong
# one. Paging a multireddit does not return strictly older posts: it merges the
# member subs' listings, so one quiet sub's older post drags a page's apparent
# age back while the dense front of the stream has barely moved. Measured on one
# 35-sub group, page by page:
#
#     page 1   0.01h to 0.30h
#     page 2   0.30h to 0.63h
#     page 3   0.63h to 6.25h     <- one outlier, not real depth
#     page 4   0.99h to 2.23h     <- back to newer again
#     page 5   1.35h to 1.71h
#
# A rule of "stop when the oldest post passes the floor" would have quit at page
# 3 believing it had six hours of cover, having actually read the newest 40
# minutes densely. That same non-monotonicity is why "pages=2 reaches back N
# hours" is not a stable number: the same group measured 0.8h at one time of day
# and 6.2h at another. Overlap does not care about ordering.
OVERLAP_STOP = 0.80


def _volume_chunks(subs: list[str], rates: dict[str, int], n: int) -> list[list[str]]:
    """Split the watchlist into groups of similar posting volume.

    Same number of groups as the alphabetical split, so the request count does
    not change, but the busiest subs get spread across them instead of landing
    together. Greedy: heaviest sub first, into whichever group is lightest.
    """
    if not subs:
        return []
    want = max(1, (len(subs) + n - 1) // n)
    groups: list[list[str]] = [[] for _ in range(want)]
    loads = [0] * want
    # Unknown subs are assumed average rather than zero, so a sub we have never
    # read does not get stacked into one group and make it the next hot spot.
    known = [v for v in rates.values() if v] or [1]
    default = sorted(known)[len(known) // 2]
    for sub in sorted(subs, key=lambda s: -rates.get(s.lower(), default)):
        i = loads.index(min(loads))
        groups[i].append(sub)
        loads[i] += rates.get(sub.lower(), default)
    return [g for g in groups if g]


def _chunks(xs: list, n: int) -> list[list]:
    return [xs[i:i + n] for i in range(0, len(xs), n)]


# ------------------------------------------------------------ the OAuth source
# Why this exists, measured on 2026-10-17 from a GitHub Actions runner in Azure
# (egress 172.184.190.243), against the same URLs this Mac uses:
#
#   www.reddit.com  /r/a+b/new/  HTML    403 Blocked
#   old.reddit.com  /r/a+b/new/  HTML    403 Blocked      (200 from home)
#   www.reddit.com  /r/a/new.json          403 Blocked
#   www.reddit.com  /r/a/new/.rss          200, but then 429 on almost every
#                                          request after the first: 2 of 24
#                                          succeeded, the rest instantly. The
#                                          feed budget is per IP and a shared
#                                          runner IP has usually spent it.
#
# So every logged-out path is dead from a datacenter, and the HTML collector
# below cannot run anywhere except a home connection. Authenticated API reads
# are the only route, which is the sanctioned one anyway: the quota is per
# client_id rather than per IP, so a shared runner address stops mattering.
#
# Nothing about collection CHANGES here. The three passes, the scoring, the
# minor guard and the person-word rule all live in collect_html and are shared,
# because the only real difference is which function hands over a page of posts.
# These replace exactly that, so there is one collector and not two to keep in
# step.
SOURCE = "html"             # "html" on a laptop, "oauth" on a runner
OAUTH_UA = "clarence-gtm-monitor/1.0 (by /u/{user}; personal project, read-only)"
_TOKEN: str | None = None
OAUTH_PAUSE = 1.0           # app-only OAuth allows 100/min; this is well inside


def set_source(name: str) -> None:
    """Choose where posts come from. Call before collect_html."""
    global SOURCE
    if name not in ("html", "oauth"):
        raise ValueError(f"unknown source {name!r}, expected html or oauth")
    SOURCE = name


def oauth_ua() -> str:
    try:
        user = json.loads(WATCH.read_text()).get("username") or "clarence"
    except Exception:
        user = "clarence"
    return OAUTH_UA.format(user=user)


def oauth_ready() -> tuple[bool, str]:
    """Can we talk to the API? Returns (ok, a line explaining why not).

    Separated out so a runner can say precisely what is missing BEFORE it
    collects, rather than reporting a successful run that found nothing.
    """
    creds = load_creds()
    if not creds:
        # Deliberately does NOT tell you to go and register an app. That is not
        # available here (see CLAUDE.md), so this source cannot run at all and
        # suggesting it only sends the reader down a dead end.
        return False, ("no Reddit app credentials, and there will not be any, so "
                       "this OAuth source cannot run. Collection is SOURCE="
                       '"html" on the always-on Mac, which is the only address '
                       "Reddit answers")
    if not oauth_token():
        # Only reachable if credentials somehow exist, which today they do not.
        return False, ("Reddit refused the client_credentials grant. The id and "
                       "secret may be the wrong way round: the id is the short "
                       "string under the app name")
    return True, "ok"


def oauth_token() -> str | None:
    """One token per process. App-only, read-only, no user password anywhere."""
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    creds = load_creds()
    if not creds:
        return None
    _TOKEN = get_token(creds, oauth_ua())
    return _TOKEN


def flag(v: object) -> bool | None:
    """A yes, a no, or nothing heard, on its way to JSON.

    Two jobs, both small and both load-bearing. It keeps None as None, so "we
    were never told" never collapses into "no". And it hands back a REAL boolean
    rather than SQLite's 1 and 0, because the board asks `t.archived === true`
    and in JavaScript `1 === true` is false: a thread read straight out of the
    store as a 1 would land in the reply queue saying you can comment on an
    archived thread, which is the whole bug this column exists to end.
    """
    return None if v is None else bool(v)


def _from_api(listing: dict | None) -> list[dict]:
    """API children, in exactly the shape parse_listing returns.

    Strictly better input than the HTML path gives: `selftext` is the whole post
    body, where a sub's /new page carries only the title. The judge sees what the
    person actually wrote, which is the difference the rubric cares about.

    It is also the only source that can say whether a thread takes a comment at
    all. `archived` and `locked` sit on every post in the listing, free, and
    were being read and dropped here until 2026-10-18.
    """
    out = []
    for child in ((listing or {}).get("data") or {}).get("children") or []:
        d = child.get("data") or {}
        pid = d.get("name") or ""
        perma = d.get("permalink") or ""
        if not pid.startswith("t3_") or "/comments/" not in perma:
            continue
        if d.get("stickied") or d.get("promoted") or d.get("over_18"):
            continue
        out.append({
            "id": pid,
            "title": d.get("title") or "",
            "url": "https://www.reddit.com" + perma,
            "subreddit": d.get("subreddit") or "",
            "author": d.get("author") or "",
            "body": d.get("selftext") or "",
            "created_ms": int(float(d.get("created_utc") or 0) * 1000),
            "num_comments": int(d.get("num_comments") or 0),
            "score": int(d.get("score") or 0),
            # Read through `flag` rather than `bool(...)` on purpose: a listing
            # shape that stopped carrying these should leave them unknown, not
            # quietly answer "you can reply to this" about every post on Reddit.
            "archived": flag(d.get("archived")),
            "locked": flag(d.get("locked")),
        })
    return out


def _oauth_multireddit_new(subs: list[str], pages: int,
                           known: set | None = None) -> list[dict]:
    """Newest across a group of subs. Multireddit paths work on the API too, so
    the request count stays the same as the HTML path: one per page per group.

    Same adaptive walk as the HTML path, for the same reason, so the two sources
    cannot drift apart on how deep they read.
    """
    token = oauth_token()
    if not token or not subs:
        return []
    names = [urllib.parse.quote(s) for s in subs]
    base = ("https://oauth.reddit.com/r/" + "+".join(names) + "/new?limit=100")
    limit = pages if known is None else MAX_PAGES
    out, after = [], ""
    for i in range(limit):
        url = base + (f"&count={i * 100}&after={after}" if after else "")
        got = _from_api(get_json(url, oauth_ua(), token))
        if not got:
            break
        out += got
        after = got[-1]["id"]
        if known is not None:
            overlap = sum(1 for d in got if d["id"] in known) / len(got)
            if overlap >= OVERLAP_STOP:
                break
        if len(got) < 90:
            break
        if i + 1 < limit:
            time.sleep(OAUTH_PAUSE)
    return out


def _oauth_search(query: str, period: str, subs: list[str] | None = None) -> list[dict]:
    """Search, which has been off since Reddit login-walled it on 2026-08-31.
    Authenticating brings it back, and with it the sitewide discovery that is the
    only way a subreddit nobody thought of finds its way onto the watchlist."""
    token = oauth_token()
    if not token:
        return []
    q = urllib.parse.quote(query)
    if subs:
        url = ("https://oauth.reddit.com/r/"
               + "+".join(urllib.parse.quote(s) for s in subs)
               + f"/search?q={q}&restrict_sr=on&sort=new&t={period}&limit=100")
    else:
        url = (f"https://oauth.reddit.com/search?q={q}&sort=new&t={period}"
               f"&limit=100&type=link")
    got = _from_api(get_json(url, oauth_ua(), token))
    time.sleep(OAUTH_PAUSE)
    return got


def load_seen() -> dict:
    if not SEEN.exists():
        return {}
    try:
        return json.loads(SEEN.read_text())
    except Exception:
        return {}


def save_seen(seen: dict) -> None:
    """Remember every post we have already judged, so we judge it once, ever."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=SEEN_TTL_DAYS)).isoformat()
    pruned = {k: v for k, v in seen.items()
              if str(v.get("first_seen", "")) >= cutoff}
    SEEN.write_text(json.dumps(pruned, indent=1))


def multireddit_new(subs: list[str], pages: int = NATIVE_PAGES,
                    known: set | None = None) -> list[dict]:
    """Newest posts across a group of subs. One fetch per page.

    Pass `known` (post ids already on file) and it stops when it has caught up
    with them instead of reading a fixed number of pages. A fixed count cannot
    be right for every group: measured on the real watchlist, two pages of the
    busiest 35-sub group covered 0.3 hours at one time of day against a job that
    runs every hour, while two pages of the nine quiet native subs covered 19
    hours. The busy group was dropping posts on every single run, silently, and
    no single page count fixes both.

    Without `known` the old fixed-page behaviour is unchanged, which is what the
    OAuth path and any caller that has no store still get.
    """
    if not subs:
        return []
    if SOURCE == "oauth":
        return _oauth_multireddit_new(subs, pages, known)
    names = [urllib.parse.quote(s) for s in subs]
    if len(names) == 1:
        names = names * 2          # r/x+x, so www keeps us on the legacy renderer
    base = LISTING_HOST + "/r/" + "+".join(names) + "/new/?limit=100"
    limit = pages if known is None else MAX_PAGES
    out, after = [], ""
    for i in range(limit):
        url = base + (f"&count={i * 100}&after={after}" if after else "")
        got = parse_listing(fetch_html(url))
        if not got:
            break
        out += got
        after = got[-1]["id"]
        if known is not None:
            overlap = sum(1 for d in got if d["id"] in known) / len(got)
            if overlap >= OVERLAP_STOP:
                break               # caught up with what we already have
        if len(got) < 90:
            break                   # the listing ran out before we did
        if i + 1 < limit:
            time.sleep(PAUSE)
    return out


# Reddit's search is gone for logged-out readers as of 2026-08-31. old.reddit
# login-walls it, and every www shape (sitewide, restrict_sr, with or without the
# trailing slash) returns an 8KB JavaScript shell with no results in the HTML.
# Left switched off rather than deleted: the parsers and callers are correct, and
# the day an authenticated session or a rendered fetch exists, this flips back to
# True and discovery resumes. While it is False the run skips about 30 requests
# per lane that could only ever fail, which is also 90 seconds of PAUSE saved.
# The cost of it being off: no sitewide discovery, so new subreddits stop finding
# their way in on their own and the watchlist only grows by hand.
SEARCH_OK = False
_search_warned = False


def _search_unavailable() -> list:
    global _search_warned
    if not _search_warned:
        _search_warned = True
        print("  · search skipped: Reddit serves no results to logged-out readers "
              "since 2026-08-31, so only the watched subs are read this run")
    return []


def group_search(subs: list[str], query: str, period: str) -> list[dict]:
    """Search a theme inside a group of subs. One fetch, body excerpts included."""
    if SOURCE == "oauth":
        return _oauth_search(query, period, subs)
    if not SEARCH_OK:
        return _search_unavailable()
    url = ("https://old.reddit.com/r/" + "+".join(urllib.parse.quote(s) for s in subs)
           + "/search?q=" + urllib.parse.quote(query)
           + f"&restrict_sr=on&sort=new&t={period}&include_over_18=off")
    return parse_search(fetch_html(url))


def sitewide_search(query: str, period: str) -> list[dict]:
    """The same theme across ALL of Reddit. This is how new subs get found."""
    if SOURCE == "oauth":
        return _oauth_search(query, period)
    if not SEARCH_OK:
        return _search_unavailable()
    url = ("https://old.reddit.com/search?q=" + urllib.parse.quote(query)
           + f"&sort=new&t={period}&include_over_18=off")
    return parse_search(fetch_html(url))


# Words that mean a human being is involved. A theme phrase only counts when one
# of these sits near it, which is the difference between "lost touch with an old
# roommate" and "lost touch with what our clients want".
PEOPLE = ("friend", "friendship", "buddy", " pal ", "family", "sister", "brother",
          "sibling", "mom ", "mother", "dad ", "father", "parents", "cousin",
          "roommate", "classmate", "girlfriend", "boyfriend", "group chat",
          "people i", "someone i", "my ex", "loved one", "relatives", "cousins")


# Reddit is full of teenagers, and general subs surface them constantly ("17f",
# "I'm 15 and my only friend"). A founder replying to a distressed minor to build
# presence for a product is not somewhere we go, and no ranking should ever have
# to be trusted to catch it. This runs before anything else does.
MINOR_RE = re.compile(
    r"\b(?:1[0-7]\s?[mfMF]|[mfMF]\s?1[0-7])\b"                 # 17f, f17, 16 M
    r"|\bi(?:'m| am)\s+(?:1[0-7]|thirteen|fourteen|fifteen|sixteen|seventeen)\b"
    r"|\b(?:1[0-7])\s*(?:years?\s*old|yo|y/o)\b"
    # Present tense only. "my high school best friend, we are both 30 now" is an
    # adult remembering, and that is the single best kind of post we get.
    r"|\bi(?:'m| am)\s+(?:still\s+)?(?:in\s+)?(?:middle|high)\s?school\b"
    r"|\b(?:freshman|sophomore|junior|senior)\s+(?:in|at)\s+high\s?school\b"
    r"|\bmy\s+(?:mom|dad|parents)\s+(?:won'?t let me|say i can'?t)\b", re.I)


def reads_as_a_minor(text: str) -> bool:
    """True when the poster reads like a kid. Deliberately errs toward yes."""
    return bool(MINOR_RE.search(text or ""))


def people_near(text: str, phrase: str, window: int = 180) -> bool:
    low = text.lower()
    i = low.find(phrase.lower())
    if i < 0:                                   # theme (not exact) match
        spots = [low.find(t) for t in phrase.lower().split() if len(t) > 3]
        spots = [x for x in spots if x >= 0]
        if not spots:
            return False
        i = min(spots)
    near = low[max(0, i - window): i + len(phrase) + window]
    return any(p in near for p in PEOPLE)


def collect_html(period: str = "week", lane: str = DEFAULT_LANE) -> list[dict]:
    """Three passes, widest net last.

      1. NATIVE subs, read in full. No keyword needed: in r/lostafriend the
         subreddit IS the signal, and a real post there often has a title like
         "what's wrong with me?" that no phrase list would ever catch.
      2. The WATCHLIST, searched by theme in groups. Big general subs where our
         people surface occasionally, so reading them in full would be all noise.
      3. SITEWIDE search, which needs no list and is how subs nobody thought of
         (r/AskWomenOver30, r/MyEx, r/whatdoIdo) end up in the queue.
    """
    w = load_watch(lane)
    clusters = w["clusters"]
    native = w.get("native_subs", [])
    watchlist = w.get("subreddits", [])
    on_the_list = {s.lower() for s in native + watchlist}
    # What each community has actually been worth, learned from the record. This
    # is the flywheel arriving in the ranking: subs you post to float, subs that
    # waste your reads sink, without anyone maintaining a list by hand.
    with store.connect() as _c:
        priors = store.sub_priors(_c, lane)
        # What we have already filed, so each group can be walked back only as
        # far as it needs to be, and how much each sub actually posts, so the
        # groups can be balanced by volume instead of by first letter.
        known = store.read_ids(_c)
        rates = store.read_volume(_c, lane)
    # The conversations lane insists a person word sits near the matched phrase.
    # Launch phrases are product phrases ("best mac app for"), where no person is
    # expected, and that guard silently threw out nearly every hit.
    needs_people = w.get("require_people_near", True)
    now = datetime.datetime.now(datetime.timezone.utc)
    seen: dict[str, dict] = {}
    read_counts: dict[str, int] = {}      # what each sub handed over this run
    read_ids: list = []                   # every id we were shown, filed or not

    def consider(d: dict, where: str, require_phrase: bool = True) -> None:
        pid = d["id"]
        if reads_as_a_minor(f"{d['title']} {d['body']}"):
            return
        m_score, cluster, phrase = match(f"{d['title']}\n{d['body']}", clusters)
        if m_score > 0.0:
            # A phrase on its own says very little: "lost touch with what our
            # clients want" is not a friendship post. Only keep it if a PERSON is
            # in the neighbourhood of the phrase, which is what these are about.
            if needs_people and not people_near(f"{d['title']}. {d['body']}", phrase):
                if require_phrase:
                    return
                m_score = 0.0
            elif match(d["title"], clusters)[0] == 0.0:
                # In the title it is what the post is ABOUT. In the body it may
                # be an aside, and search hands us only a couple of lines of body.
                m_score *= 0.75
        if m_score == 0.0:
            if require_phrase:
                return
            m_score, cluster, phrase = 0.6, "native", ""   # the sub is the signal

        if d.get("created_ms"):
            age_h = round((now.timestamp() - d["created_ms"] / 1000) / 3600, 1)
        else:
            age_h = _age_hours_from_iso(d.get("created_iso", ""), now)
        ncom = int(d.get("num_comments") or 0)
        # The free score. Everything here is arithmetic on things we already know,
        # so it can run over every post we collect, however many that becomes.
        fresh_s = max(0.0, 1.0 - age_h / 72.0)
        opp = 1.0 if ncom <= 8 else 0.5 if ncom <= 25 else 0.2
        prior = priors.get(d["subreddit"].lower(),
                           0.45 if d["subreddit"].lower() in on_the_list else 0.30)
        fit = round(0.45 * m_score + 0.20 * fresh_s + 0.15 * opp + 0.20 * prior, 3)
        prev = seen.get(pid)
        if prev and prev["fit"] >= fit:
            if d["body"] and not prev.get("snippet"):
                prev["snippet"] = d["body"][:280]
            return
        if fit < 0.3:
            return
        fresh = f"{age_h:.0f}h old" if age_h < 48 else f"{age_h / 24:.0f}d old"
        room = "few replies (room for a top answer)" if ncom <= 8 else f"{ncom} replies"
        if cluster == "native":
            why = f"r/{d['subreddit']} is all about this · {fresh} · {room}"
        else:
            hit = "exact match" if m_score >= 1.0 else "theme match"
            why = f"{cluster}: {hit} on “{phrase}” · {fresh} · {room} · {where}"
        seen[pid] = {
            "id": pid, "source": "live", "found_via": where, "lane": lane,
            "subreddit": d["subreddit"], "title": d["title"], "url": d["url"],
            # WHEN it was written, not how old it was when we happened to look.
            # Age is derived; storing only the hours made every post freeze at
            # the age it had on the run that found it, so a six-week-old thread
            # still read "4h old" and looked worth answering. The listing hands
            # us created_ms three lines up and this row used to drop it.
            "created_utc": (d["created_ms"] / 1000) if d.get("created_ms") else None,
            "age_hours": age_h, "num_comments": ncom, "score": d.get("score", 0),
            # Absent on the HTML path, and that is correct rather than a gap:
            # a sub's /new page says nothing about either, so the row carries
            # None, the store keeps NULL, and the board goes on treating the
            # thread as answerable. Only the OAuth listing above can fill these.
            "archived": d.get("archived"), "locked": d.get("locked"),
            "matched_cluster": cluster, "matched_phrase": phrase, "fit": fit,
            "why": why,
            "snippet": (d["body"][:280] + "…") if len(d["body"]) > 280 else d["body"],
        }

    # Balanced by observed volume, not alphabetically. Alphabetical order is
    # what put r/Advice, r/AskReddit, r/relationship_advice and
    # r/relationships in one group, and a group of the four busiest subs on
    # the list burns through a page of /new in minutes.
    groups = _volume_chunks(watchlist, rates, CHUNK)
    # With search gone, a watchlist sub is only reachable by READING it. That used
    # to be one search per group per theme; it is now one listing per group, and
    # the theme phrases are matched here instead of by Reddit. Fewer requests, and
    # it no longer depends on Reddit's index having caught up. The one thing lost
    # is the body excerpt search returned, so a watchlist sub matches on title.
    # `native` subs stay separate because they alone skip the phrase requirement:
    # being in one of those subs is itself the signal.
    native_groups = _volume_chunks(native, rates, CHUNK)
    steps = len(native_groups) + len(groups) + len(clusters)
    step = 0

    for grp in native_groups:
        step += 1
        posts = multireddit_new(grp, NATIVE_PAGES, known)
        for d in posts:
            read_counts[d["subreddit"].lower()] = \
                read_counts.get(d["subreddit"].lower(), 0) + 1
            read_ids.append(d["id"])
            consider(d, f"native ({len(grp)} subs)", require_phrase=False)
        print(f"  [{step}/{steps}] native subs: {len(posts)} new posts read, "
              f"{len(seen)} candidates so far")

    for gi, grp in enumerate(groups, 1):
        step += 1
        posts = multireddit_new(grp, NATIVE_PAGES, known)
        before = len(seen)
        for d in posts:
            read_counts[d["subreddit"].lower()] = \
                read_counts.get(d["subreddit"].lower(), 0) + 1
            read_ids.append(d["id"])
            consider(d, f"watchlist group {gi}", require_phrase=True)
        print(f"  [{step}/{steps}] watchlist group {gi} ({len(grp)} subs): "
              f"{len(posts)} read, {len(seen) - before} on theme, "
              f"{len(seen)} candidates so far")
        time.sleep(PAUSE)

    for name, phrases in clusters.items():
        step += 1
        q = _cluster_query(phrases)
        found = sitewide_search(q, period)
        if len(found) < 5 and period == "week":
            # A quiet week for a theme is not a reason to come back empty. Widen
            # to the month, freshness scoring still floats the new ones up.
            time.sleep(PAUSE)
            found = sitewide_search(q, "month")
        for d in found:
            consider(d, "sitewide search")
        print(f"  [{step}/{steps}] sitewide × “{name}”: {len(found)} hits, "
              f"{len(seen)} candidates so far")
        time.sleep(PAUSE)

    if read_counts:
        with store.connect() as _c:
            store.record_read_volume(_c, lane, read_counts)
            store.record_reads(_c, read_ids)

    # No cap. Everything collected gets filed, because the point of the store is
    # that a good thread found on Tuesday is still there on Friday. What gets
    # capped is the expensive part: how many of these a model looks at.
    return sorted(seen.values(), key=lambda t: -t["fit"])


# ------------------------------------------------------------------- triage
# The phrase matching above is built for RECALL: it would rather hand you a
# stranger's sales post than miss the person who is quietly losing their oldest
# friend. This pass is the other half, PRECISION. One cheap model call reads the
# whole shortlist and answers the only question that matters: is there a real
# person here, talking about a real relationship, that you could genuinely help?
# It is optional. No key, no problem: the queue just keeps the phrase ranking.
CONVERSATIONS_RUBRIC = (
    "You triage a shortlist of Reddit threads for a founder who replies to people "
    "by hand, as himself. He is building a Mac app that helps people keep up with "
    "the friends and family they care about. He is NOT looking for customers to "
    "pitch. He is looking for threads where he has something real and useful to "
    "say, from lived experience.\n\n"
    "THE ONE THING THAT MATTERS: his subject is KEEPING IN TOUCH. People who still "
    "like each other and cannot keep it up, usually because somebody moved or life "
    "got busy. Score against that, not against how emotional or dramatic the post "
    "is.\n\n"
    "SCORE 0.85 to 1.0, the cream. The practical struggle of staying in touch:\n"
    "  - friends who moved away, to another city, state or country, and the "
    "friendship is fading with the distance\n"
    "  - asking how other people do it: how do you keep in touch, how often do you "
    "text your friends, how do you keep up as an adult, what actually works\n"
    "  - admitting they are bad at it: terrible at replying, months go by, they "
    "think about people and never message, and it is costing them friendships\n"
    "  - life stage pulled everyone apart (graduated, moved for work, had kids) and "
    "they want to hold on to the people anyway\n"
    "  - asking for a system, habit, app or trick to remember to reach out\n\n"
    "SCORE 0.5 to 0.8, good but softer: a friendship quietly drifting with nobody "
    "at fault, wanting to message someone after a long silence and feeling weird "
    "about it, loneliness that comes from distance rather than conflict.\n\n"
    "DROP, these are relationship advice and not his subject, however sad or "
    "popular the post is:\n"
    "  - friendship fallout: a falling out, a betrayal, an ex-best friend, being "
    "cut off or ghosted after a fight, a toxic or one-sided friendship\n"
    "  - should I confront them, do I owe them a conversation, am I the problem, "
    "who was in the wrong\n"
    "  - romantic relationships, break-ups, dating, crushes, situationships\n"
    "  - making brand new friends from scratch, meetups, or finding a social circle "
    "somewhere new, when there is nobody they are trying to keep\n"
    "  - anything selling or promoting (apps, testers, services, subscriptions), "
    "dating or hookup personals, and AITA-style judge-me drama\n"
    "  - the phrase matched an aside: losing touch with a SKILL, a market, a job or "
    "a hobby rather than a person\n\n"
    "DROP, without exception, any post whose author reads like a minor: a stated "
    "age under 18, school-age framing, or a voice that is plainly a kid. He does "
    "not reply to children, however sad the post is. When in doubt, drop it.\n\n"
    "DROP posts not written in English, or written for a local community he could "
    "not credibly join. A Taglish thread in a Filipino sub can be perfectly on "
    "topic and still be somewhere he has no business replying.\n\n"
    "Some posts arrive with the title and subreddit only, no body. Judge those on "
    "what the title and the community tell you.\n"
    "Return STRICT JSON only, no prose around it."
)


LAUNCH_RUBRIC = (
    "You triage Reddit threads for a founder who is about to put his Mac app in "
    "front of people. The app helps you keep up with the friends and family you "
    "care about: it reads your own messages on your Mac and drafts replies in your "
    "voice. This lane is the opposite of his other one. Here he IS allowed to talk "
    "about what he built, but only where that is welcome, and never as a drive-by "
    "advert.\n\n"
    "SCORE 0.85 to 1.0: someone is actually shopping. They ask for an app or a way "
    "to remember to text people, to keep track of friends, to stay in touch, to "
    "handle messages from their Mac, or they ask for Mac app recommendations in a "
    "sub where recommending is normal. Also: a show-what-you-built thread where "
    "posting his own project is the point of the sub.\n"
    "SCORE 0.5 to 0.8: someone describing the problem in a product-shopping frame "
    "(\"is there anything that does X\"), or a genuine discussion of private, "
    "on-device or local AI where how he built this is a real contribution.\n"
    "DROP: plain tech support (iMessage will not sync, my Mac is broken), other "
    "founders' launch and marketing-meta posts (how do I get downloads, roast my "
    "landing page) where he would add nothing, anything Android or Windows only, "
    "recruiting and hiring, and any thread where mentioning his app would plainly "
    "read as spam to the people in it.\n"
    "DROP anything not in English, and any post whose author reads like a minor.\n"
    "The test for a high score is simple: would the person in that thread be GLAD "
    "someone told them this exists? If you have to argue for it, score it low.\n"
    "Return STRICT JSON only, no prose around it."
)


RUBRICS = {"conversations": CONVERSATIONS_RUBRIC, "launch": LAUNCH_RUBRIC}


def _triage_batch(batch: list[dict], key: str, model: str,
                  rubric: str = None) -> dict[int, dict]:
    """Judge one batch. Returns {position in batch: verdict}. Never raises."""
    lines = []
    for i, t in enumerate(batch, 1):
        # The real post when `bodies.py` has managed to fetch it, the listing
        # preview otherwise. Prefer the real one and give it more room: the whole
        # point of fetching it was that a title plus 220 characters is a thin
        # thing to judge a person on, and the judge was being handed the thin
        # version even once the full text was on file.
        full = (t.get("post_text") or "").strip().replace("\n", " ")
        snip = (t.get("snippet") or "").replace("\n", " ")[:220]
        # Posts read from a sub's /new listing have no body text available. Say so
        # rather than showing an empty field, which reads as "this person wrote
        # nothing" and gets a good thread thrown out.
        body = (full[:1200] if len(full) > len(snip)
                else (snip if snip else
                      "(not pulled, judge from the title and the subreddit)"))
        lines.append(f'{i}. r/{t["subreddit"]} · {t["age_hours"]}h old · '
                     f'{t["num_comments"]} replies\n   TITLE: {t["title"]}\n'
                     f'   BODY: {body}')
    user = ("Threads:\n\n" + "\n\n".join(lines) +
            '\n\nReturn a JSON array with one object per thread, in order:\n'
            '[{"n": 1, "keep": true, "score": 0.0, "why": "one short line, in plain '
            'words, saying what this person is actually going through"}]\n'
            'The "why" is what he reads in his queue, so write it for a human, not '
            'a label. No em dash anywhere.')
    payload = {"model": model, "max_tokens": 4000,
               "system": rubric or CONVERSATIONS_RUBRIC,
               "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    usage = {"in": 0, "out": 0}
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as r:
            data = json.loads(r.read().decode())
        text = "".join(b.get("text", "") for b in data.get("content", []))
        u = data.get("usage") or {}
        usage = {"in": int(u.get("input_tokens", 0)),
                 "out": int(u.get("output_tokens", 0))}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        print(f"  ! triage batch failed (HTTP {e.code}: {detail or e.reason})")
        # Record WHY, not just that it broke. "Collected 295, judged 0" reads like
        # a quiet day; "your credit balance is too low" is a thing you can fix.
        try:
            _c = store.connect()
            store.note(_c, "judge_error", f"HTTP {e.code}: {detail or e.reason}")
            _c.close()
        except Exception:
            pass
        return {}, usage
    except Exception as e:
        print(f"  ! triage batch failed ({e})")
        return {}, usage
    verdicts = []
    whole = re.search(r"\[.*\]", text, re.S)
    if whole:
        try:
            verdicts = json.loads(whole.group(0))
        except Exception:
            verdicts = []
    if not verdicts:
        # A truncated reply still has complete objects in it. Take those.
        for obj in re.findall(r"\{[^{}]*\}", text, re.S):
            try:
                verdicts.append(json.loads(obj))
            except Exception:
                pass
    got = {int(v["n"]): v for v in verdicts
           if isinstance(v, dict) and str(v.get("n", "")).isdigit()}
    if len(got) < len(batch):
        print(f"  ! triage judged {len(got)} of {len(batch)} in this batch; "
              f"the rest stay in, unjudged")
    return got, usage


# Conversations raised from 60 on 2026-11-07, with the native list going from
# 9 rooms to 19. Every post in a native room is a candidate, so the pool the
# free score has to sort through roughly trebled, and 60 slots spread over a
# noisier pool is how the focused rooms get crowded out by the big general
# ones. At a hundredth of a cent each this costs about fifteen cents a day.
JUDGE_BUDGET = {"conversations": 120, "launch": 30}


# Scoring a post against a rubric is classification, and classification is what
# the small model is for. Measured on 2026-10-17 against 71 posts the big model
# had already judged: 91% agreement on the keep-or-drop call at 1/20th the
# price ($0.000088 a post against $0.0018). The disagreements were not spread
# evenly, which is the useful part: they sat almost entirely between 0.30 and
# 0.55, where the big model was marginal too. The cheap model is slightly
# stricter, so left alone it drops about one keeper in seven.
CHEAP_JUDGE = "claude-haiku-5-5"
STRONG_JUDGE = "claude-sonnet-5"
# So: everything gets the cheap judge, and only the band it is unsure about is
# paid for twice. That band is small, which is why this costs a fraction of
# judging everything with the big model and loses almost nothing.
ESCALATE_LOW, ESCALATE_HIGH = 0.30, 0.60


def _escalate(con, lane: str, key: str, rows: list[dict], batch_size: int) -> int:
    """Re-judge the uncertain band with the stronger model, and let it overrule.

    Only reached for posts the cheap judge put in the middle. A post it was
    confident about, either way, is not worth paying twice for.
    """
    if not rows:
        return 0
    spent, n = 0.0, 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        got, usage = _triage_batch(batch, key, STRONG_JUDGE, RUBRICS.get(lane))
        spent += store.record_spend(con, lane, STRONG_JUDGE,
                                    usage["in"], usage["out"], len(batch))
        for i, t in enumerate(batch, 1):
            v = got.get(i)
            if v is None:
                continue
            score = float(v.get("score", 0.5) or 0)
            why = (v.get("why") or "").strip().replace("\u2014", ", ")
            store.record_verdict(con, t["id"], score,
                                 bool(v.get("keep", True)) and score >= 0.35, why)
            n += 1
        con.commit()
    if spent:
        print(f"  second look at {n} borderline posts: ${spent:.3f}")
    return n


def judge_unjudged(con, lane: str, budget: int, model: str | None = None,
                   batch_size: int = 12) -> int:
    """Spend the model on the best slice the free score found, and only that.

    This is the line that lets the watchlist grow to hundreds of subreddits
    without the bill growing with it. Collection is free and unbounded; judging
    is capped, and the cap is spent on whatever currently looks most promising.
    Every verdict is written back, so nothing is ever judged twice.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    todo = store.unjudged(con, lane, budget)
    if not todo:
        return 0
    if not key:
        print("  no ANTHROPIC_API_KEY: nothing judged, the free ranking still stands")
        return 0
    model = model or os.environ.get("ANTHROPIC_MODEL", CHEAP_JUDGE)
    judged, spent = 0, 0.0
    borderline: list[dict] = []
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        for t in batch:                       # the judge prompt wants these names
            t.setdefault("age_hours", 0.0)
            t.setdefault("num_comments", 0)
        got, usage = _triage_batch(batch, key, model, RUBRICS.get(lane))
        spent += store.record_spend(con, lane, model, usage["in"], usage["out"], len(batch))
        for i, t in enumerate(batch, 1):
            v = got.get(i)
            if v is None:
                continue
            if judged == 0:
                store.clear_note(con, "judge_error")   # it works again
            judged += 1
            # An escape, not the character: scripts/no-emdash.sh covers this
            # file, and the literal would flag the line that enforces the rule.
            why = (v.get("why") or "").strip().replace("\u2014", ", ")
            score = float(v.get("score", 0.5) or 0)
            store.record_verdict(con, t["id"], score,
                                 bool(v.get("keep", True)) and score >= 0.35, why)
            if model == CHEAP_JUDGE and ESCALATE_LOW <= score <= ESCALATE_HIGH:
                borderline.append(t)
        con.commit()
    if borderline:
        _escalate(con, lane, key, borderline, batch_size)
    if spent:
        print(f"  spent ${spent:.3f} judging {judged} posts "
              f"(${spent / max(judged, 1):.4f} each)")
    return judged


def attach_policy(threads: list[dict], lane: str) -> list[dict]:
    """Tell each launch card what you are actually allowed to do in that sub."""
    if lane != "launch":
        return threads
    policy = load_watch(lane).get("sub_policy", {})
    lookup = {sub.lower(): rule for rule, subs in policy.items() for sub in subs}
    labels = {"show_your_work": "post your own project here, that is the point",
              "flair_and_be_honest": "self-promo allowed, flair it and say you built it",
              "answer_only_if_asked": "do NOT post your app, only answer someone who asked"}
    for t in threads:
        rule = lookup.get(t["subreddit"].lower())
        t["post_rule"] = rule or "unlisted sub, read the sidebar before you post"
        t["post_rule_says"] = labels.get(rule, "read the sidebar first, once")
    return threads


def subs_report(min_seen: int = 3, lane: str = DEFAULT_LANE) -> dict:
    """Which subreddits actually pay off, straight from the record.

    Every post is filed under the community it came from, so the watchlist never
    has to stay a guess. Subs producing keepers that are not on the list are the
    ones to add. Subs on the list with nothing to show across a real sample are
    the ones to drop. Once you have posted a few times, `worth` also carries what
    YOUR decisions say, which outranks anything the model thinks.
    """
    w = load_watch(lane)
    watched = {s.lower() for s in w.get("subreddits", []) + w.get("native_subs", [])}
    con = store.connect()
    priors = store.sub_priors(con, lane)
    cur = con.execute(
        "SELECT subreddit sub, COUNT(*) collected, "
        "SUM(llm_score IS NOT NULL) seen, SUM(COALESCE(keep, 0)) kept, "
        "SUM(status = 'posted') posted, SUM(status = 'skip') skipped "
        "FROM posts WHERE lane = ? GROUP BY subreddit", (lane,))
    rows = []
    for r in cur.fetchall():
        sub = r["sub"] or ""
        rows.append({"sub": sub, "collected": r["collected"], "seen": r["seen"] or 0,
                     "kept": r["kept"] or 0, "posted": r["posted"] or 0,
                     "skipped": r["skipped"] or 0,
                     "watched": sub.lower() in watched,
                     "worth": round(priors.get(sub.lower(), 0.0), 2),
                     "hit_rate": round((r["kept"] or 0) / r["seen"], 2) if r["seen"] else 0.0})
    st = store.stats(con)
    con.close()
    rows.sort(key=lambda r: (-r["posted"], -r["kept"], -r["worth"]))
    add = [r for r in rows if not r["watched"] and r["kept"] >= 1]
    drop = [r for r in rows if r["watched"] and r["seen"] >= min_seen and r["kept"] == 0]
    return {"subs": rows, "suggest_add": add, "suggest_drop": drop,
            "n_watched": len(watched), "n_judged": sum(r["seen"] for r in rows),
            "store": st}


def find_subs(question: str, period: str = "year", phrases: list[str] | None = None,
              lane: str = DEFAULT_LANE) -> dict:
    """Where on Reddit are people asking this? Answered from behaviour.

    Point it at a real question ("how do you keep in touch with friends who moved
    away"), it works out the phrases people actually write, searches all of Reddit
    for each, and tallies WHICH COMMUNITIES the matching posts live in.

    Behaviour beats the directory here. Reddit's own subreddit search matches sub
    names and blurbs, so asking it about "keeping in touch with friends" returns an
    anime sub called "I Became Friends With the Second Cutest Girl in My Class".
    Posts do not lie about where the conversation actually happens.
    """
    w = load_watch(lane)
    watched = {s.lower() for s in w.get("subreddits", []) + w.get("native_subs", [])}
    q = (question or "").strip()
    if not q:
        return {"question": q, "subs": [], "n_posts": 0, "n_subs": 0, "phrases": []}
    phrases = phrases or phrasings(q)
    hits: dict[str, dict] = {}
    n_posts = 0
    for phrase in phrases:
        url = ("https://old.reddit.com/search?q=" + urllib.parse.quote(f'"{phrase}"')
               + f"&sort=relevance&t={period}&include_over_18=off")
        found = parse_search(fetch_html(url))
        n_posts += len(found)
        for d in found:
            sub = d["subreddit"]
            if not sub:
                continue
            row = hits.setdefault(sub.lower(), {
                "sub": sub, "hits": 0, "phrases": [],
                "watched": sub.lower() in watched, "samples": []})
            row["hits"] += 1
            if phrase not in row["phrases"]:
                row["phrases"].append(phrase)
            if len(row["samples"]) < 3:
                row["samples"].append({"title": d["title"], "url": d["url"],
                                       "num_comments": d["num_comments"]})
        time.sleep(PAUSE)
    # A sub that shows up for SEVERAL different phrasings is a place the whole
    # conversation lives, not a place one post happened to land.
    rows = sorted(hits.values(), key=lambda r: (-len(r["phrases"]), -r["hits"]))
    # Only suggest a sub the conversation actually LIVES in. One hit on one
    # phrasing is how r/rarepuppers ends up on a watchlist, because somebody
    # captioned a dog photo "adopted him from my friend who moved away".
    worth_it = [r for r in rows
                if not r["watched"] and (len(r["phrases"]) >= 2 or r["hits"] >= 2)]
    return {"question": q, "phrases": phrases, "n_posts": n_posts,
            "n_subs": len(rows), "subs": rows, "new_subs": worth_it}


def dedupe_crossposts(threads: list[dict]) -> list[dict]:

    """The same person posting the same thing to three subs should take one slot."""
    out, seen = [], set()
    for t in sorted(threads, key=lambda x: -x["fit"]):
        key = (re.sub(r"\W+", "", t["title"].lower())[:60],
               re.sub(r"\W+", "", (t.get("snippet") or "").lower())[:80])
        if key[0] and key in seen:
            prev = next(x for x in out
                        if (re.sub(r"\W+", "", x["title"].lower())[:60],
                            re.sub(r"\W+", "", (x.get("snippet") or "").lower())[:80]) == key)
            prev.setdefault("also_in", []).append(t["subreddit"])
            continue
        seen.add(key)
        out.append(t)
    return out


def build_queue(con, lanes: list[str], top_pct: float = 0.10) -> list[dict]:
    """The queue is a VIEW, not a run's output: the best of everything you hold
    that you have not acted on yet. A thread found on Tuesday is still here on
    Friday, which is the whole point of keeping a store."""
    now_s = time.time()
    out = []
    for lane in lanes:
        rows = store.queue(con, lane, top_pct=top_pct)
        mine: list[dict] = []
        for r in rows:
            # Age is worked out NOW, from when the post was written. Storing it as
            # a number of hours is what made a six-day-old thread still say "12.4h".
            # Posts collected before created_utc was stored have a FROZEN age: the
            # hours-old number from the run that found them. Serving that number
            # later says "4h old" about a thread that is six weeks dead, which is
            # the one fact that decides whether answering it is worth anything.
            # Add the time that has passed since we last saw it, which is a true
            # lower bound, and mark it approximate so the board can say so.
            age_approx = False
            if r["created_utc"]:
                age_h = (now_s - r["created_utc"]) / 3600
            else:
                age_h = r["age_hours"] or 0
                seen = r["last_seen"] or r["first_seen"]
                if seen:
                    try:
                        then = datetime.datetime.fromisoformat(seen)
                        if then.tzinfo is None:
                            then = then.replace(tzinfo=datetime.timezone.utc)
                        elapsed = (datetime.datetime.now(datetime.timezone.utc)
                                   - then).total_seconds() / 3600
                        if elapsed > 0:
                            age_h += elapsed
                            age_approx = True
                    except (ValueError, TypeError):
                        pass
            fit = round(0.65 * (r["llm_score"] or 0) + 0.35 * (r["pre_score"] or 0), 3)
            # A thread worth answering three days ago usually is not now: the
            # conversation has moved on and a reply lands under thirty others.
            if age_h > 48:
                fit = round(fit * max(0.35, 1.0 - (age_h - 48) / 240), 3)
            mine.append({
                "id": r["id"], "lane": lane, "source": "live",
                "subreddit": r["subreddit"], "title": r["title"], "url": r["url"],
                "snippet": r["snippet"] or "", "age_hours": round(age_h, 1),
                "age_approx": age_approx, "stale": age_h > 72,
                "num_comments": r["num_comments"] or 0, "score": r["score"] or 0,
                "found_via": r["found_via"], "matched_cluster": r["matched_cluster"],
                "matched_phrase": r["matched_phrase"], "post_rule": r["post_rule"],
                "llm_score": r["llm_score"], "fit": fit,
                "status": r["status"] or "todo",
                # Can you even reply. True, False, or null when nothing has told
                # us, which the board reads as "still worth trying".
                "archived": flag(r.get("archived")),
                "locked": flag(r.get("locked")),
                "author": r["author"] or "",
                "prospect": bool(r["prospect"]),
                "why": r["why"] or "", "first_seen": r["first_seen"],
                "is_new": r["first_seen"] == r["last_seen"],
            })
        # Only this lane's rows. Applying it to everything collected so far is how
        # a posting rule ends up on a conversations card, where there is no app to
        # post and the line is just noise.
        out += attach_policy(mine, lane)
    return sorted(out, key=lambda t: -t["fit"])


def main(lanes: list[str] | None = None, top_pct: float = 0.10,
         source: str | None = None, budget: dict | None = None) -> int:
    load_env()
    if source:
        set_source(source)
    lanes = lanes or all_lanes()
    con = store.connect()
    for lane in lanes:
        w = load_watch(lane)
        n = len(w.get("native_subs", [])) + len(w.get("subreddits", []))
        print(f"\n=== {lane}: {n} subreddits, {len(w['clusters'])} themes, "
              f"via {SOURCE} ===")
        found = collect_html(lane=lane)
        for t in found:
            t["pre_score"] = t.pop("fit")
        fresh, again = store.upsert(con, found)
        print(f"  filed {len(found)} posts: {fresh} new, {again} seen before")
        cap = (budget or JUDGE_BUDGET).get(lane, 40)
        judged = judge_unjudged(con, lane, cap)
        print(f"  judged the top {judged} by free score")
    threads = dedupe_crossposts(build_queue(con, lanes, top_pct))
    QUEUE.write_text(json.dumps(
        {"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "lanes": lanes,
         "top_pct": top_pct, "count": len(threads), "threads": threads}, indent=2))
    st = store.stats(con)
    con.close()
    by_lane = {ln: sum(1 for t in threads if t.get("lane") == ln) for ln in lanes}
    print(f"\nQueue: {len(threads)} threads {by_lane}")
    print(f"Store: {st['total']} posts across {st['subs']} subreddits, "
          f"{st['judged']} judged, {st['kept']} worth answering, "
          f"{st['posted']} posted by you, {st['skipped']} skipped")
    return 0


if __name__ == "__main__":
    LANE = (sys.argv[sys.argv.index("--lane") + 1]
            if "--lane" in sys.argv else DEFAULT_LANE)
    if "--find" in sys.argv:
        i = sys.argv.index("--find")
        question = " ".join(sys.argv[i + 1:]).strip()
        if not question:
            print('usage: monitor.py --find "how do you keep in touch with friends who moved"')
            raise SystemExit(1)
        load_env()                       # the phrasings step wants the API key too
        r = find_subs(question, lane=LANE)
        print(f'\n“{r["question"]}”\n{r["n_posts"]} matching posts across '
              f'{r["n_subs"]} subreddits\n')
        print("searched: " + " · ".join(f'“{p}”' for p in r["phrases"]) + "\n")
        for row in r["subs"][:25]:
            mark = "  " if row["watched"] else " +"
            print(f'{mark} r/{row["sub"]:28s} {row["hits"]:3d} posts '
                  f'across {len(row["phrases"])} phrasings')
            for smp in row["samples"][:2]:
                print(f'        “{smp["title"][:66]}”')
        if r["new_subs"]:
            print("\nNOT ON YOUR LIST YET:")
            print("  " + ", ".join(f'r/{x["sub"]}' for x in r["new_subs"][:20]))
        raise SystemExit(0)
    if "--subs" in sys.argv:
        load_env()
        r = subs_report(lane=LANE)
        print(f"{r['n_judged']} posts judged across {len(r['subs'])} subreddits "
              f"({r['n_watched']} on the watchlist)\n")
        print("  kept/seen  subreddit")
        for row in r["subs"][:40]:
            mark = " " if row["watched"] else "+"
            print(f"  {row['kept']:3d}/{row['seen']:-3d}   {mark} r/{row['sub']}")
        if r["suggest_add"]:
            print("\nWORTH ADDING (producing keepers, not on your list):")
            print("  " + ", ".join(f"r/{x['sub']}" for x in r["suggest_add"]))
        if r["suggest_drop"]:
            print("\nWORTH DROPPING (on your list, nothing to show for it):")
            print("  " + ", ".join(f"r/{x['sub']}" for x in r["suggest_drop"]))
        raise SystemExit(0)
    raise SystemExit(main([LANE] if "--lane" in sys.argv else None))
