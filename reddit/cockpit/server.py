#!/usr/bin/env python3
"""Clarence GTM cockpit. One local dashboard for the growth loop.

Two things to do each week, front and center:
  - Is it working: the plain-English insights, the AEO trend over time, and
    traffic, on one screen.
  - Do the work: the ranked Reddit reply queue, with a draft box and
    todo/drafted/posted/skip tracking per thread.

Everything else stays available under "more": the gap list, the winners
drill-down, full traffic/country detail, the sources repository, the ICP
signals corpus, and the voices/personas. Same endpoints, just not up front.

    python3 reddit/cockpit/server.py             # then open http://localhost:4360

Reads the existing AEO ledger (scratch/aeo) and the Reddit monitor output
(scratch/reddit/queue.json). Stdlib only, no build step. It never posts to
Reddit; it finds and drafts, you post.
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent


def _repo_root() -> Path:
    """The checkout this board runs in, worked out from this file's position.

    This file is version controlled now, so the board is not one copy on one
    laptop. Its data is not, and must not be: state.json, the store and
    reddit_creds.json live under scratch/, which git ignores. So code and data
    part company here, the way store._data_dir splits the collector from its
    database.

    Both the new home (reddit/cockpit) and the old one (scratch/cockpit) sit two
    levels under the checkout, so the arithmetic is the same from either, and a
    machine that has not flipped its symlinks yet still finds its data. Deriving
    it beats reading an env var: a variable nobody set would point the data dir
    at an empty directory beside the code, and an empty state.json reads exactly
    like a morning with nothing in the queue.
    """
    return HERE.parents[1]


REPO = _repo_root()                     # the git checkout: code, in version control
ROOT = REPO / "scratch"                 # the data side: never in version control
# The AEO runtime moved OUT of the repo to ~/Library/Application Support on
# 2026-07-14 (launchd + Full Disk Access; see that dir's MOVED.md). Prefer the
# live runtime; fall back to the in-repo pre-move copy only if it's absent.
LIVE_AEO = Path.home() / "Library" / "Application Support" / "clarence-aeo"
REPO_AEO = REPO / "aeo"          # the tracked copy GitHub Actions commits to
# The weekly sweep now runs in the cloud and commits its results to the repo copy,
# so prefer that (git pull to refresh). Fall back to the local runtime dir, then
# the old in-repo scratch copy. The cockpit reads only committed data (results.csv
# + sources.json), so it no longer needs raw/ transcripts to be present here.
if (REPO_AEO / "results.csv").exists():
    AEO = REPO_AEO
elif (LIVE_AEO / "results.csv").exists():
    AEO = LIVE_AEO
else:
    AEO = ROOT / "aeo"
REDDIT = ROOT / "reddit"
WEB = HERE / "web"
# The phone board is one file served from two places, here and the Cloudflare
# Worker, so it lives with the cloud half rather than being copied into this
# directory. Beside this file is the fallback, for a checkout from before the
# cloud half moved into the repo.
M_HTML = REPO / "reddit" / "cloud" / "public" / "m.html"
STATE = REDDIT / "state.json"
PORT = int(os.environ.get("COCKPIT_PORT", "4360"))   # a second instance can take another port
# Loopback by default: this board has no login on it, and it can spend money by
# calling the model. Set COCKPIT_HOST=0.0.0.0 on the always-on machine to read it
# from another computer on your own network, and only on a network you trust.
HOST = os.environ.get("COCKPIT_HOST", "127.0.0.1")
# Loopback normally implies "already on this machine", which is why it needs no
# key. That stops being true the moment a tunnel (Tailscale serve, cloudflared)
# forwards to loopback on your behalf: then the whole tailnet arrives here as
# 127.0.0.1. Set COCKPIT_REQUIRE_KEY=1 in that setup to keep the key on, so the
# tunnel's own auth is not the only thing standing in front of the board.
REQUIRE_KEY = os.environ.get("COCKPIT_REQUIRE_KEY", "") == "1"

# When the board is on the network it needs a key, because it is not a passive
# dashboard: it can spend money on the model and open pull requests on the repo.
# Loopback needs nothing, since reaching it already means being on the machine.
TOKEN_FILE = Path.home() / "Library" / "Application Support" / "clarence-aeo" / "cockpit-key"


def cockpit_token() -> str:
    if HOST in ("127.0.0.1", "localhost") and not REQUIRE_KEY:
        return ""
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    import secrets
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok + "\n")
    TOKEN_FILE.chmod(0o600)
    return tok


TOKEN = cockpit_token()

sys.path.insert(0, str(AEO))
try:
    import sources as aeo_sources        # reuse the citation miner
except Exception:
    aeo_sources = None
try:
    import trend as aeo_trend            # reuse the time-frame analyzer
except Exception:
    aeo_trend = None
sys.path.insert(0, str(REDDIT))
try:
    import enrich as reddit_enrich       # scrape + emotional mining + draft
except Exception:
    reddit_enrich = None
try:
    import monitor as reddit_monitor     # discovery, triage, the subs payoff report
except Exception:
    reddit_monitor = None
CORPUS = REDDIT / "icp_corpus.jsonl"

URL_RE = re.compile(r"https?://[^\s\"'<>)\]}]+")
# Search-engine grounding wrappers / redirects, not real content sources.
NOISE = ("vertexaisearch.cloud.google.com", "googleusercontent.com",
         "grounding-api-redirect", "bing.com", "google.com/search")
_aeo_cache: dict = {}  # holds "sig" -> (run, mtime) and "out" -> computed digest


def _is_noise(host: str) -> bool:
    return any(n in host for n in NOISE)


_sources_cache: dict = {}


def load_sources() -> list:
    """The committed citation catalog (sources.json), reloaded when it changes.
    Reading this instead of mining raw/ is what lets the cockpit run off the data
    GitHub Actions commits (raw transcripts stay a build artifact, not in git)."""
    p = AEO / "sources.json"
    if not p.exists():
        return []
    mt = p.stat().st_mtime_ns
    if _sources_cache.get("sig") != mt:
        try:
            _sources_cache["data"] = json.loads(p.read_text()).get("sources", [])
        except Exception:
            _sources_cache["data"] = []
        _sources_cache["sig"] = mt
    return _sources_cache["data"]


def pages_for_domain(domain: str, limit: int = 20) -> list:
    """Top cited pages on one domain, ranked by how many of our questions cite it."""
    dom = domain.lower().lstrip(".")
    src = [s for s in load_sources() if (s.get("domain") or "").lower() == dom]
    src.sort(key=lambda s: (-int(s.get("n_queries") or 0), s.get("url", "")))
    return src[:limit]


_SUBREDDIT_RE = re.compile(r"reddit\.com/r/([^/]+)/", re.I)


def _subreddit_of(url: str) -> str:
    m = _SUBREDDIT_RE.search(url or "")
    return m.group(1) if m else ""


# ---------- AEO ----------
def _latest_run(rows: list[dict]) -> str:
    return sorted({r["run_date"] for r in rows})[-1]


def compute_aeo() -> dict:
    results = AEO / "results.csv"
    if not results.exists():
        return {"error": "no AEO ledger yet. Run scratch/aeo/probe.py"}
    rows = list(csv.DictReader(results.open()))
    if not rows:
        return {"error": "AEO ledger is empty"}
    run = _latest_run(rows)
    # Cache by (latest run date, ledger mtime). Keying on the date alone meant a
    # re-run or a resumed sweep on the SAME day kept serving the first, partial
    # digest (e.g. a mid-crawl 16-answer snapshot) until the server restarted.
    # Folding in the file's mtime busts the cache the moment the ledger changes.
    sig = (run, results.stat().st_mtime_ns)
    if _aeo_cache.get("sig") == sig:
        return _aeo_cache["out"]

    latest = [r for r in rows if r["run_date"] == run]
    ok = [r for r in latest if r["ok"] == "1"]
    providers = sorted({r["provider"] for r in ok})
    queries = sorted({r["query"] for r in latest})
    mentioned_pairs = {(r["query"], r["provider"]) for r in ok if r["mentioned"] == "1"}
    mentioned_qs = {q for q, _ in mentioned_pairs}
    absent = sorted(q for q in queries if q not in mentioned_qs)

    # The exact search + engine behind every mention/citation, for research:
    # which query, which platform, the cited URL, and a path to the raw answer.
    mentions_detail = sorted(
        [{"query": r["query"], "provider": r["provider"], "raw": r.get("raw_path", "")}
         for r in ok if r["mentioned"] == "1"],
        key=lambda d: (d["query"], d["provider"]))
    cites_detail = sorted(
        [{"query": r["query"], "provider": r["provider"],
          "url": r.get("clarence_url", ""), "raw": r.get("raw_path", "")}
         for r in ok if r.get("clarence_cited") == "1"],
        key=lambda d: (d["query"], d["provider"]))

    # who's winning: tally the cited domains across this run's answers, straight
    # from the ledger's cited_domains column. No raw/ files needed, so it works
    # off the committed data the cloud job writes.
    domains: dict[str, int] = defaultdict(int)
    for r in ok:
        seen = set()
        for host in (r.get("cited_domains") or "").split(";"):
            host = host.strip().lower()
            if host.startswith("www."):
                host = host[4:]
            if host and host not in seen and not _is_noise(host):
                seen.add(host)
                domains[host] += 1
    winners = sorted(domains.items(), key=lambda kv: -kv[1])[:15]

    # the most recent FULL run (the ~175-query sweep), for the gap-list toggle,
    # since the latest run is usually the small weekly watchlist
    runs_q: dict[str, set] = defaultdict(set)
    for r in rows:
        runs_q[r["run_date"]].add(r["query"])
    full_run = next((d for d in sorted(runs_q, reverse=True)
                     if len(runs_q[d]) >= 60), None)
    full_absent, full_n = [], 0
    if full_run and full_run != run:
        fr = [r for r in rows if r["run_date"] == full_run]
        fok = [r for r in fr if r["ok"] == "1"]
        fq = sorted({r["query"] for r in fr})
        fhit = {r["query"] for r in fok if r["mentioned"] == "1"}
        full_absent = sorted(q for q in fq if q not in fhit)
        full_n = len(fq)

    reddit_threads = []
    subs_counter: dict[str, int] = defaultdict(int)
    for s in pages_for_domain("reddit.com", 12):
        qs = s.get("queries") or []
        reddit_threads.append({
            "title": s.get("title") or s.get("url", ""),
            "url": s.get("url", ""),
            "n_questions": int(s.get("n_queries") or len(qs)),
            "example": sorted(qs)[0] if qs else "",
        })
        sub = _subreddit_of(s.get("url", ""))
        if sub:
            subs_counter[sub] += int(s.get("n_queries") or 1)
    subs = sorted(subs_counter.items(), key=lambda kv: -kv[1])[:8]

    out = {
        "run": run,
        "providers": providers,
        "n_answers": len(ok),
        "n_queries": len(queries),
        "queries": queries,
        "n_mentions": len(mentioned_pairs),
        "mentions_detail": mentions_detail,
        "cites_detail": cites_detail,
        "n_absent": len(absent),
        "absent": absent,
        "full_run": full_run if full_run != run else None,
        "full_absent": full_absent,
        "full_n": full_n,
        "winners": winners,
        "reddit_threads": reddit_threads,
        "reddit_subs": subs,
    }
    _aeo_cache["sig"] = sig
    _aeo_cache["out"] = out
    return out


def top_pages_for(domain: str) -> dict:
    """The drill-down under 'who's winning': the exact PAGES cited on any
    platform (reddit, facebook, youtube, apps.apple.com, …), ranked by how many
    of our questions point at each."""
    dom = domain.lower().lstrip(".")
    is_reddit = "reddit" in dom
    pages = []
    subs_counter: dict[str, int] = defaultdict(int)
    for s in pages_for_domain(dom, 20):
        qs = s.get("queries") or []
        pages.append({"title": s.get("title") or s.get("url", ""),
                      "url": s.get("url", ""),
                      "n_questions": int(s.get("n_queries") or len(qs)),
                      "example": sorted(qs)[0] if qs else ""})
        if is_reddit:
            sub = _subreddit_of(s.get("url", ""))
            if sub:
                subs_counter[sub] += int(s.get("n_queries") or 1)
    out = {"domain": domain, "pages": pages}
    if is_reddit and subs_counter:
        out["subs"] = sorted(subs_counter.items(), key=lambda kv: -kv[1])[:10]
    return out


# ---------- Runs + cost log ----------
def compute_runs() -> dict:
    results = AEO / "results.csv"
    if not results.exists():
        return {"runs": [], "total": 0}
    rows = [r for r in csv.DictReader(results.open())
            if re.match(r"^\d{4}-\d{2}-\d{2}$", (r.get("run_date") or "").strip())]
    cost_by_run, total = {}, 0
    cf = AEO / "cost.json"
    if cf.exists():
        try:
            cj = json.loads(cf.read_text())
            cost_by_run = {r["run"]: r for r in cj.get("runs", [])}
            total = cj.get("total", 0)
        except Exception:
            pass
    agg: dict = defaultdict(lambda: {"providers": set(), "queries": set(),
                                     "answers": 0, "mentions": 0, "cites": 0})
    for r in rows:
        a = agg[r["run_date"]]
        a["providers"].add(r["provider"])
        a["queries"].add(r["query"])
        if r.get("ok") == "1":
            a["answers"] += 1
        if r.get("mentioned") == "1":
            a["mentions"] += 1
        if r.get("clarence_cited") == "1":
            a["cites"] += 1
    out = []
    for d in sorted(agg, reverse=True):
        a, c = agg[d], cost_by_run.get(d, {})
        out.append({"run": d, "providers": sorted(a["providers"]),
                    "questions": len(a["queries"]), "answers": a["answers"],
                    "mentions": a["mentions"], "cites": a["cites"],
                    "cost": c.get("cost")})
    return {"runs": out, "total": total,
            "schedule": "Mondays 9am, full 175-question sweep on all four engines"}


def read_raw(rel: str) -> dict:
    """Serve one saved answer file for research, path-confined to raw/. Cloud runs
    keep raw transcripts as a GitHub build artifact rather than in the repo, so look
    in both the primary data dir and the local runtime dir; if neither has it, say
    so plainly instead of a bare 'not found'."""
    if not rel:
        return {"error": "no path"}
    for base in (AEO, LIVE_AEO):
        root = (base / "raw").resolve()
        try:
            f = (base / rel).resolve()
        except Exception:
            continue
        if str(f).startswith(str(root)) and f.exists():
            return {"path": rel, "text": f.read_text(errors="ignore")[:20000]}
    return {"error": "This answer transcript isn't stored locally. Cloud sweeps keep the "
                     "raw answers as a downloadable artifact on the GitHub run (Actions "
                     "tab), not in the repo. Locally-run sweeps show the full text here."}


# ---------- Weekly content plan ----------
def _content_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:70]


def _written_answer_qs() -> set:
    """Questions we've already drafted an /answers page for, so we skip them."""
    have: set = set()
    ad = REPO / "site" / "src" / "content" / "answers"
    if ad.exists():
        have |= {p.stem for p in ad.glob("*.md")}
    wf = ROOT / "content" / "written.json"
    if wf.exists():
        try:
            have |= set(json.loads(wf.read_text()).get("queries", []))
        except Exception:
            pass
    return have


_CONTENT_DOMS = ("reddit.com", "wikihow", "medium", "socialself", "psychologytoday",
                 "verywellmind", "bustle", "succeedsocially", "deeperconvos",
                 "theguardian", "quora", "substack")
_GENERIC_DOMS = ("youtube.", "apps.apple", "play.google", "facebook.", "instagram.",
                 "tiktok.", "google.", "bing.")


def _winning_page_for(query: str) -> dict:
    """The page to out-answer for a question we're absent from (from sources.json).
    Prefer the Reddit thread winning it (that's the out-answer strategy), then real
    content pages; a generic video/store/social result is not a useful target, so
    it ranks last. Ties break on how many of our questions cite the page."""
    cands = [s for s in load_sources() if query in (s.get("queries") or [])]
    if not cands:
        return {}

    def rank(s: dict):
        dom = (s.get("domain") or "").lower()
        if "reddit.com" in dom:
            tier = 0
        elif any(c in dom for c in _CONTENT_DOMS):
            tier = 1
        elif any(g in dom for g in _GENERIC_DOMS):
            tier = 3
        else:
            tier = 2
        return (tier, -int(s.get("n_queries") or 0))

    return sorted(cands, key=rank)[0]


def compute_content_plan(n_blog: int = 6, n_reddit: int = 6) -> dict:
    """This week's content ideas, assembled from data we already have:
    - GUIDE ideas: top gap questions (absent everywhere, non-branded, not yet
      drafted), each paired with the page currently winning it (to out-answer).
    - REDDIT ideas: top live commentable threads in the queue (to engage with).
    The founder reviews, then one-clicks a draft on the ones worth doing. Nothing
    is written or posted here; drafting is a separate, explicit action."""
    aeo = compute_aeo()
    written = _written_answer_qs()
    blog = []
    for q in aeo.get("absent", []):
        if "clarence" in q.lower():
            continue
        if _content_slug(q) in written or q in written:
            continue
        wp = _winning_page_for(q)
        blog.append({
            "question": q,
            "winning_title": wp.get("title", ""),
            "winning_url": wp.get("url", ""),
            "winning_domain": wp.get("domain", ""),
            "winning_n": int(wp.get("n_queries") or 0),
        })
        if len(blog) >= n_blog:
            break

    st = {}
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text())
        except Exception:
            st = {}
    done = {"posted", "skip"}
    reddit = []
    qf = REDDIT / "queue.json"
    if qf.exists():
        try:
            threads = json.loads(qf.read_text()).get("threads", [])
        except Exception:
            threads = []
        for t in threads:
            if t.get("archived"):
                continue
            if (st.get(t.get("url", ""), {}) or {}).get("status") in done:
                continue
            reddit.append({
                "id": t.get("id", ""), "title": t.get("title", ""),
                "url": t.get("url", ""), "subreddit": t.get("subreddit", ""),
                "cluster": t.get("matched_cluster", ""),
                "why": t.get("why", ""), "fit": t.get("fit"),
                "num_comments": t.get("num_comments"),
            })
            if len(reddit) >= n_reddit:
                break

    return {"run": aeo.get("run"), "blog": blog, "reddit": reddit,
            "counts": {"gap_pool": len([q for q in aeo.get("absent", [])
                                        if "clarence" not in q.lower()]),
                       "queue_pool": len(reddit)}}


def draft_guide(query: str) -> dict:
    """One-click a guide draft for a gap question: runs the content generator
    (drafts the /answers page, runs the em-dash gate) and opens a one-tap-merge
    PR. Never merges; the founder reviews and merges."""
    if not query:
        return {"ok": False, "error": "no query"}
    gen = ROOT / "content" / "generate.py"
    if not gen.exists():
        return {"ok": False, "error": "generate.py not found"}
    try:
        r = subprocess.run([sys.executable, str(gen), "--query", query, "--pr"],
                           cwd=str(REPO), capture_output=True, text=True,
                           timeout=240)
        tail = (r.stdout or "")[-1200:] + (("\n" + r.stderr[-600:]) if r.stderr else "")
        return {"ok": r.returncode == 0, "output": tail}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- Idea backlog (content/ideas.py, the tracked repo copy) ----------
# The persistent, editable content pipeline that sits above the auto /answers
# job: guide + blog ideas tracked from idea -> published. The store and its seed
# live in the REPO's content/ dir (not scratch/content, which is the old
# pre-move copy), so the cockpit and the cloud read the same file.
_BACKLOG = None


def _backlog():
    """Lazy-import the tracked backlog module. Returns the module or None."""
    global _BACKLOG
    if _BACKLOG is None:
        import importlib
        cdir = str(REPO / "content")
        if cdir not in sys.path:
            sys.path.insert(0, cdir)
        try:
            _BACKLOG = importlib.import_module("ideas")
        except Exception as e:
            print("backlog import failed:", e)
            _BACKLOG = False
    return _BACKLOG or None


_OUTREACH = None


def _outreach():
    """Lazy-import the First-10 outreach tracker (sibling module). Returns it or None."""
    global _OUTREACH
    if _OUTREACH is None:
        import importlib
        try:
            _OUTREACH = importlib.import_module("outreach")
        except Exception as e:
            print("outreach import failed:", e)
            _OUTREACH = False
    return _OUTREACH or None


def compute_ideas() -> dict:
    mod = _backlog()
    if not mod:
        return {"error": "content/ideas.py not importable", "ideas": []}
    try:
        data = mod.load()
    except Exception as e:
        return {"error": str(e), "ideas": []}
    return {"ideas": data.get("ideas", []),
            "statuses": list(mod.STATUSES), "kinds": list(mod.KINDS)}


def draft_longform(idea_id: str) -> dict:
    """Draft a backlog card via longform.py (guide -> guides, blog -> draft
    scaffold) and open a PR. Shelled out so the model call + git worktree never
    block or pollute the server process. longform.py marks the card in-pr."""
    lf = REPO / "content" / "longform.py"
    if not lf.exists():
        return {"ok": False, "error": "longform.py not found"}
    try:
        r = subprocess.run([sys.executable, str(lf), "--idea", idea_id],
                           cwd=str(REPO), capture_output=True, text=True,
                           timeout=300)
        tail = (r.stdout or "")[-1500:] + (("\n" + r.stderr[-600:])
                                           if r.stderr else "")
        return {"ok": r.returncode == 0, "output": tail}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- Trend (AEO over time) ----------
def compute_trend() -> dict:
    if aeo_trend is None:
        return {"error": "trend.py not found next to the AEO ledger"}
    try:
        rows = list(aeo_trend.load())
        runs = aeo_trend.by_run(rows)
        seen = aeo_trend.first_seen(rows)
    except Exception as e:
        return {"error": f"trend unavailable: {e}"}
    series = []
    for r in runs:
        nq, nhit = len(r["questions"]), len(r["hit_questions"])
        series.append({
            "date": r["date"], "providers": sorted(r["providers"]),
            "answers": r["answers"], "mentions": r["mentions"],
            "questions": nq, "questions_mentioned": nhit,
            "coverage": round(100 * nhit / nq, 1) if nq else 0,
            "citations": r["citations"],
        })
    named = sorted(({"date": d, "query": q} for q, d in seen.items()),
                   key=lambda x: (x["date"], x["query"]))
    return {"runs": series, "named": named}


# ---------- Insights (plain-English "what this means") ----------
def _branded(q: str) -> bool:
    return "clarence" in (q or "").lower()


def compute_insights() -> dict:
    """Turn the raw AEO numbers into plain sentences a human can act on.
    Deterministic, no LLM: reads the trend + the latest run's citation detail."""
    if aeo_trend is None:
        return {"lines": []}
    try:
        rows = list(aeo_trend.load())
        runs = aeo_trend.by_run(rows)
    except Exception as e:
        return {"error": str(e), "lines": []}
    if not runs:
        return {"lines": []}
    latest = runs[-1]
    date = latest["date"]
    lr = [r for r in rows if r["run_date"] == date]
    cited = [(r["query"], r.get("clarence_url", "")) for r in lr
             if r.get("clarence_cited") == "1"]
    cited_cat = sorted({q for q, _ in cited if not _branded(q)})
    mentioned_q = sorted({r["query"] for r in lr if r.get("mentioned") == "1"})
    nq, nhit = len(latest["questions"]), len(latest["hit_questions"])
    absent = nq - nhit
    scope = "weekly watchlist" if nq < 60 else "full query set"

    # nearest earlier run of the SAME scope, so the comparison is fair
    prev = next((r for r in reversed(runs[:-1]) if len(r["questions"]) == nq), None)

    lines = []
    lines.append({"tone": "plain", "text":
        f"This run is the {scope} ({nq} questions). Only compare it to other "
        f"{scope} runs. Held next to the big runs, the percentages look like "
        f"they jumped when really only the scope changed."})
    if prev:
        dm = latest["mentions"] - prev["mentions"]
        word = "the same" if dm == 0 else (f"up {dm}" if dm > 0 else f"down {abs(dm)}")
        lines.append({"tone": "plain", "text":
            f"Versus the last comparable run ({prev['date']}): you were named in "
            f"{latest['mentions']} answers, {word}. Coverage held at "
            f"{round(100*nhit/nq,1)}% ({nhit} of {nq} questions)."})
    if cited_cat:
        qs = ", ".join(f'“{q}”' for q in cited_cat)
        lines.append({"tone": "good", "text":
            f"The real news: {len(cited_cat)} answer(s) LINKED to your site on "
            f"questions that do not contain your name ({qs}). That is the first "
            f"sign an AI engine is discovering you as a source, not just echoing "
            f"your brand. It lines up with unblocking the AI crawlers."})
    elif cited:
        lines.append({"tone": "watch", "text":
            f"You got {len(cited)} link citation(s), but only on your own-name "
            f"searches. Real, yet it is your brand echoing back, not new discovery."})
    else:
        lines.append({"tone": "watch", "text":
            "No answer linked to your site this run."})
    if mentioned_q and all(_branded(q) for q in mentioned_q):
        lines.append({"tone": "watch", "text":
            f"In the answer text you are still only named on own-name searches. "
            f"On the {absent} category questions people actually ask (no brand "
            f"name), you are absent."})
    lines.append({"tone": "plain", "text":
        "What to watch next Monday: the Cites column (is discovery growing?) and "
        "the 'absent everywhere' count (is it shrinking?). Shipping the "
        "Reddit-flanking blog posts is what moves them."})
    headline = ("First sign of organic discovery" if cited_cat
                else "Holding steady, still brand-only")
    return {"run": date, "compared_to": prev["date"] if prev else None,
            "headline": headline, "lines": lines}


# ---------- Traffic (Cloudflare over time) ----------
def compute_traffic() -> dict:
    daily = AEO / "cf_daily.csv"
    if not daily.exists():
        return {"error": "no Cloudflare data yet. Add CLOUDFLARE_API_TOKEN to "
                          "the AEO .env and run cloudflare.py"}
    series = []
    for r in csv.DictReader(daily.open()):
        try:
            series.append({"date": r["date"], "requests": int(r["requests"]),
                           "page_views": int(r["page_views"]),
                           "uniques": int(r["uniques"])})
        except (ValueError, KeyError):
            continue
    countries, generated = [], None
    cfile = AEO / "cf_countries.json"
    if cfile.exists():
        try:
            snap = json.loads(cfile.read_text())
            countries = snap.get("countries", [])
            generated = snap.get("generated")
        except Exception:
            pass
    return {"series": series, "countries": countries, "generated": generated}


# ---------- Sources repository ----------
SOURCES_FILE = AEO / "sources.json"
SAVED_FILE = AEO / "saved.json"


def load_saved() -> dict:
    if SAVED_FILE.exists():
        try:
            return json.loads(SAVED_FILE.read_text())
        except Exception:
            return {}
    return {}


def compute_sources() -> dict:
    if not SOURCES_FILE.exists():
        return {"error": "no sources repository yet. Run sources_repo.py", "sources": []}
    repo = json.loads(SOURCES_FILE.read_text())
    saved = load_saved()
    out = []
    for s in repo.get("sources", []):
        entry = saved.get(s["url"], {})
        out.append({
            "url": s["url"], "domain": s["domain"], "platform": s["platform"],
            "title": s["title"], "n_queries": s["n_queries"], "answers": s["answers"],
            "first_seen": s["first_seen"], "last_seen": s["last_seen"],
            "queries": s["queries"][:6],
            "saved": bool(entry.get("saved")), "note": entry.get("note", ""),
        })
    platforms = sorted({s["platform"] for s in out})
    return {"n_sources": repo.get("n_sources", len(out)),
            "built_from_runs": repo.get("built_from_runs", []),
            "platforms": platforms, "saved_count": sum(1 for s in out if s["saved"]),
            "sources": out}


def save_source(url: str, saved: bool | None, note: str | None) -> None:
    data = load_saved()
    entry = data.get(url, {})
    if saved is not None:
        entry["saved"] = saved
    if note is not None:
        entry["note"] = note
    if not entry.get("saved") and not entry.get("note"):
        data.pop(url, None)
    else:
        data[url] = entry
    SAVED_FILE.write_text(json.dumps(data, indent=2))


# ---------- Voices + personas ----------
CONTENT = ROOT / "content"


def compute_voices() -> dict:
    out = {"voices": [], "personas": []}
    vf, pf = CONTENT / "voices.json", CONTENT / "personas.json"
    if vf.exists():
        try:
            out["voices"] = json.loads(vf.read_text()).get("voices", [])
        except Exception:
            pass
    if pf.exists():
        try:
            out["personas"] = json.loads(pf.read_text()).get("personas", [])
        except Exception:
            pass
    return out


def compute_authors() -> dict:
    """Who you write AS (the founder persona + any others) for Reddit drafting."""
    pf = CONTENT / "personas.json"
    if pf.exists():
        try:
            return {"authors": json.loads(pf.read_text()).get("authors", [])}
        except Exception:
            pass
    return {"authors": []}


# ---------- Reddit ----------
# The card writes several fields at once (paste, angle, draft, status) and the
# server is threaded, so two overlapping saves would read the same state and the
# second would drop the first one's field. One lock, held across read and write.
_STATE_LOCK = threading.Lock()


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2))


def load_corpus() -> list[dict]:
    if not CORPUS.exists():
        return []
    rows = []
    for line in CORPUS.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def corpus_view() -> dict:
    """The accumulating ICP research: aggregate pains / wants / themes / quotes
    across every thread analyzed, most-common first."""
    rows = load_corpus()
    agg = {"pain_points": defaultdict(int), "what_they_want": defaultdict(int),
           "emotional_themes": defaultdict(int)}
    quotes = []
    for r in rows:
        for field in agg:
            for item in r.get(field, []):
                agg[field][item.strip()] += 1
        for q in r.get("verbatim_quotes", []):
            quotes.append({"quote": q, "url": r.get("url"), "title": r.get("title")})
    top = {k: sorted(v.items(), key=lambda kv: -kv[1])[:20] for k, v in agg.items()}
    return {"n_threads": len(rows), "quotes": quotes[:60], **top}


def machine_power() -> dict:
    """Is the machine that runs this thing plugged in?

    Sleep is disabled so a closed lid keeps it collecting, and that switch is
    global: unplugged, it runs the battery flat and dies rather than napping.
    That is worth saying on the board, because the failure is invisible until
    everything has already stopped.
    """
    def build():
        ok, out = _run(["pmset", "-g", "batt"], ROOT, timeout=6)
        if not ok or not out:
            return {}
        ac = "AC Power" in out
        pct = re.search(r"(\d+)%", out)
        left = re.search(r"(\d+:\d+) remaining", out)
        ok2, sl = _run(["pmset", "-g"], ROOT, timeout=6)
        awake = bool(ok2 and re.search(r"SleepDisabled\s+1", sl or ""))
        return {"on_ac": ac, "percent": int(pct.group(1)) if pct else None,
                "time_left": left.group(1) if left else None, "sleep_disabled": awake,
                "at_risk": awake and not ac}
    return _cached_swr("power", 120, build)


def done_count() -> int:
    """How many you have settled, ever. The button that opens them was only shown
    after you decided something in THIS browser session, so a reload made your own
    history look like it had never been recorded."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import store as _store
        _con = _store.connect()
        n = _con.execute("SELECT COUNT(*) c FROM posts "
                         "WHERE status IN ('posted','skip')").fetchone()["c"]
        _con.close()
        return n
    except Exception:
        return 0


ALLOWED_FETCH_HOSTS = ("reddit.com", "www.reddit.com", "old.reddit.com",
                       "np.reddit.com")


def _reddit_url_ok(url: str) -> bool:
    """The only thing this server has any business fetching is Reddit.

    Several endpoints take a URL from the request body and go and get it. Left
    open, that turns the cockpit into a proxy for anything it can reach,
    including things on this machine that are not exposed to the network.
    """
    try:
        u = urlparse((url or "").strip())
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    host = (u.hostname or "").lower()
    return host in ALLOWED_FETCH_HOSTS


def reddit_done() -> dict:
    """What you have already settled. Out of the workspace, still on the record."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import store as _store
        _con = _store.connect()
        rows = [dict(r) for r in _con.execute(
            "SELECT id, subreddit, title, url, status, status_reason, status_note, "
            "acted_at FROM posts WHERE status IN ('posted','skip') "
            "ORDER BY acted_at DESC LIMIT 200")]
        _con.close()
        return {"count": len(rows), "threads": rows}
    except Exception as e:
        return {"count": 0, "threads": [], "error": str(e)}


def reddit_health() -> dict:
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import store as _store
        _con = _store.connect()
        h = _store.health(_con)
        _con.close()
        return h
    except Exception as e:
        return {"ok": False, "judge_error": f"could not read the store: {e}"}


def reddit_queue(depth: float = 0.10, within: float = 0.0,
                 order: str = "best") -> dict:
    """Cached briefly: the page fires several requests at once on load, and this
    walks the whole store each time. Twenty seconds is long enough to serve one
    page load from one build, short enough that acting on a card feels instant."""
    return _cached(f"reddit_queue:{depth}:{within}:{order}", 20,
                   lambda: _build_reddit_queue(depth, within, order))


def _build_reddit_queue(depth: float, within: float = 0.0,
                        order: str = "best") -> dict:
    """The queue, built live from the store so the depth control is instant.

    The stored queue.json is only what the last collector run published. Reading
    the store directly means "show me everything" does not have to wait an hour
    for the next run.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import store as _store
        import monitor as _mon
        _con = _store.connect()
        lanes = _mon.all_lanes()
        # Take the window FIRST, then the top slice of it. "The best 10% of the
        # last day" is a different, and more useful, list than "the best 10% of
        # everything, minus anything old".
        threads = _mon.dedupe_crossposts(
            _mon.build_queue(_con, lanes, 1.0 if within else depth))
        if within:
            threads = [t for t in threads if (t.get("age_hours") or 0) <= within]
            n = max(5, round(len(threads) * depth)) if depth < 1 else len(threads)
            threads = threads[:n]
        if order == "newest":
            threads.sort(key=lambda t: t.get("age_hours") or 0)
        pool = sum(len(_store.queue(_con, ln, top_pct=1.0)) for ln in lanes)
        _con.close()
        state = load_state()
        for t in threads:
            # The STORE owns status. state.json used to as well, and a stale entry
            # there would paint a card as skipped while the store still served it,
            # so a skip looked like it had not stuck. It keeps your drafts only.
            st = state.get(t["id"], {})
            t["draft"] = st.get("draft", "")
            t["post_text"] = st.get("post_text", "")
            t["angle"] = st.get("angle", "")
            t["comments"] = st.get("comments", [])
        return {"generated": None, "count": len(threads), "threads": threads,
                "pool": pool, "depth": depth, "done": done_count(),
                "replies": local_replies()}
    except Exception as e:
        # Say what actually broke. Falling through to the stored file silently is
        # how a stale process looked like a permissions problem for ten minutes.
        print(f"  ! live queue build failed ({type(e).__name__}: {e}), "
              f"falling back to queue.json")
        return _reddit_queue_from_file()


def note_event(post_id, kind: str, detail: dict | None = None) -> None:
    """Record one workflow step. Done on the SERVER rather than trusted to the
    page, so the trail is complete even when a phone loses the connection
    halfway through, and so it cannot be skipped by a reload."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import store as _store
        con = _store.connect()
        _store.log_event(con, post_id, kind, detail)
        con.close()
    except Exception as e:
        print(f"  ! could not record {kind}: {e}")


def capture_step(post_id, step: str, text: str | None, **kw) -> None:
    """Keep the WORDS of one step, where note_event keeps only its shape.

    Separate from note_event because the two have different jobs and different
    reasons to exist. Events measure the workflow and are safe to keep forever
    at any volume. This keeps real text, and it is kept for one purpose: the
    drafter learns his voice from replies he actually sent, and there is no way
    to learn that from a character count.

    Done on the server, for the same reason the events are: a step that depends
    on the page staying open is a step that goes missing exactly when the queue
    is being worked quickly, which is when the good examples get written.
    """
    if not (text or "").strip():
        return
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import store as _store
        con = _store.connect()
        if not kw.get("subreddit") and post_id:
            row = con.execute("SELECT subreddit FROM posts WHERE id = ?",
                              (post_id,)).fetchone()
            if row:
                kw["subreddit"] = row["subreddit"]
        _store.capture(con, post_id, step, text, **kw)
        con.close()
    except Exception as e:
        print(f"  ! could not capture {step}: {e}")


def voice_stats() -> dict:
    """Where the voice set stands, for the board rather than a terminal.

    Read only and cheap. Never raises: a missing count must not be able to take
    the queue down with it, because the queue is the thing you came here to work.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import store as _store
        import voice as _voice
        con = _store.connect()
        st = _voice.stats(con)
        con.close()
        return st
    except Exception as e:
        print(f"  ! could not read the voice set: {e}")
        return {}


def board_can() -> dict:
    """What THIS board can do, for the phone view that also runs at the edge.

    One `m.html` is served by this server and by the Cloudflare Worker, so it
    asks instead of inferring. Inferring is what it used to do, from
    `window.isSecureContext`, which answers a question about the browser and not
    about the server: over https at the edge that test passes and there is still
    no transcriber on the other end.

    transcribe: this machine has Whisper bundled and transcribes on the device.

    fetch_threads: reading a thread live needs Reddit to answer a logged-out
    request. Measured on 2026-10-17 from a home connection, it does not: the
    per-thread Atom feed answers 403, the .json answers 403, and old.reddit
    answers its login page. The only path left is the Reddit API, so this is true
    exactly when app credentials exist. Said honestly, the board offers the paste
    box instead of a button that cannot work.
    """
    ok = False
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import monitor as _mon
        ok = bool(_mon.oauth_ready()[0])
    except Exception:
        ok = False
    return {"can_transcribe": True, "can_fetch_threads": ok}


def local_replies(limit: int = 100) -> list[dict]:
    """Replies to your comments, for the board on this machine.

    The cloud Worker answers this from its own copy; this is the same list read
    straight from the store, so `m.html` renders identically whether it is being
    served from here or from the edge. Missing table just means the replies
    feature has never run, which is not an error.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import store as _store
        con = _store.connect()
        rows = con.execute(
            "SELECT r.id, r.post_id, r.subreddit, r.author, r.body, r.url,"
            " r.created_at, r.acknowledged,"
            " p.title AS post_title, p.url AS post_url, p.snippet AS post_snippet"
            " FROM replies r LEFT JOIN posts p ON p.id = r.post_id"
            " WHERE COALESCE(r.acknowledged, 0) = 0"
            " ORDER BY r.created_at DESC LIMIT ?", (limit,)).fetchall()
        con.close()
        # Your own comment is the other half of the conversation, and the drafter
        # needs it or it writes an opening line at someone mid-thread. It lives in
        # state.json under the POST id, which is what the reply joins on.
        state = load_state()
        asked = _asked_map()
        out = []
        for x in rows:
            d = dict(x)
            st = state.get(d.get("post_id") or "", {})
            d["my_comment"] = st.get("draft", "")
            d["post_text"] = st.get("post_text", "")
            a = asked.get((d.get("author") or "").lower())
            d["asked_at"] = a["at"] if a else None
            d["ask_outcome"] = a["outcome"] if a else None
            out.append(d)
        return out
    except Exception:
        return []


def _asked_map() -> dict:
    """Everyone already asked, keyed by lowercased reddit handle."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import store as _store
        con = _store.connect()
        out = _store.asked_authors(con)
        con.close()
        return out
    except Exception:
        return {}


def _reddit_queue_from_file() -> dict:
    qfile = REDDIT / "queue.json"
    if not qfile.exists():
        return {"generated": None, "threads": [], "note": "no queue yet. Hit Refresh"}
    data = json.loads(qfile.read_text())
    state = load_state()
    for t in data.get("threads", []):
        st = state.get(t["id"], {})
        t["status"] = st.get("status", "todo")
        t["draft"] = st.get("draft", "")
        t["post_text"] = st.get("post_text", "")
        t["angle"] = st.get("angle", "")
    data["replies"] = local_replies()
    return data


# ==========================================================================
# Marketing cockpit: live ops board, schedule, PRs, activity, cost, signals.
# Everything below reads real data and is read-only. Where a source is not yet
# wired, it says so plainly rather than inventing a number.
# ==========================================================================
SITE = REPO / "site"
ANSWERS = REPO / "site" / "src" / "content" / "answers"
CONTENT_WRITTEN = ROOT / "content" / "written.json"   # scratch/content/written.json

# GitHub fires scheduled workflows from the DEFAULT branch, not whatever branch
# happens to be checked out here. So the honest "what will actually run" comes
# from origin/main (fall back to main, then the working tree). We parse the cron
# from there, never from the possibly-stale checked-out copy.
SCHED_REF_CANDIDATES = ("origin/main", "main")

_cache: dict = {}   # generic ttl cache: key -> (expires_at, value)


def _cached_swr(key: str, ttl: float, fn):
    """Serve what we have, refresh behind you.

    Some of these panels ask the network (wrangler shelling out to Cloudflare,
    gh hitting GitHub), which can take the better part of ten seconds. Making the
    page wait on that is the wrong trade: a number a minute old, now, beats a
    fresh one after the header has sat blank. Only the very first call, on a cold
    process, has to wait, and startup warms it.
    """
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    if hit:
        if not _refreshing.get(key):
            _refreshing[key] = True

            def refresh():
                try:
                    _cache[key] = (time.time() + ttl, fn())
                except Exception:
                    pass
                finally:
                    _refreshing.pop(key, None)

            threading.Thread(target=refresh, daemon=True).start()
        return hit[1]                      # stale, but instant
    return _cached(key, ttl, fn)           # nothing yet, so we do have to wait


_refreshing: dict = {}


def _cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    val = fn()
    _cache[key] = (now + ttl, val)
    return val


def _run(cmd: list[str], cwd: Path, timeout: int = 30) -> tuple[bool, str]:
    """Run a read-only shell command, capture stdout. Never raises."""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout)
        return (r.returncode == 0, r.stdout if r.returncode == 0 else (r.stderr or r.stdout))
    except Exception as e:
        return (False, str(e))


def _gh_json(args: list[str]):
    """gh CLI returning JSON, or None if gh is unavailable / errors."""
    ok, out = _run(["gh", *args], REPO, timeout=25)
    if not ok:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def _git_show(path: str):
    """Contents of a repo path from the default branch (origin/main), so the
    schedule reflects what GitHub actually runs, not the checked-out branch."""
    for ref in SCHED_REF_CANDIDATES:
        ok, out = _run(["git", "show", f"{ref}:{path}"], REPO, timeout=15)
        if ok and out.strip():
            return ref, out
    # last resort: the working tree copy (may be a stale branch)
    p = REPO / path
    if p.exists():
        return "working tree", p.read_text()
    return None, ""


def _git_workflow_files() -> tuple[str, list[str]]:
    for ref in SCHED_REF_CANDIDATES:
        ok, out = _run(["git", "ls-tree", "-r", "--name-only", ref,
                        ".github/workflows"], REPO, timeout=15)
        if ok and out.strip():
            files = [l.strip() for l in out.splitlines()
                     if l.strip().endswith((".yml", ".yaml"))]
            if files:
                return ref, files
    wd = REPO / ".github" / "workflows"
    if wd.exists():
        return "working tree", [f".github/workflows/{p.name}" for p in
                                sorted(wd.glob("*.y*ml"))]
    return None, []


def _local_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).astimezone()


# ---------- cron parsing (enough for the crons we run) ----------
def _cron_expand(field: str, lo: int, hi: int) -> set:
    if field == "*":
        return set(range(lo, hi + 1))
    out: set = set()
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/")
            step = int(step)
            if base == "*":
                rng = range(lo, hi + 1)
            elif "-" in base:
                a, b = map(int, base.split("-")); rng = range(a, b + 1)
            else:
                rng = range(int(base), hi + 1)
            out |= {x for x in rng if (x - lo) % step == 0}
        elif "-" in part:
            a, b = map(int, part.split("-")); out |= set(range(a, b + 1))
        else:
            try:
                out.add(int(part))
            except ValueError:
                pass
    return out


def _parse_cron(cron: str) -> dict | None:
    bits = cron.split()
    if len(bits) != 5:
        return None
    mn, hr, dom, mon, dow = bits
    return {"min": _cron_expand(mn, 0, 59), "hour": _cron_expand(hr, 0, 23),
            "dom": _cron_expand(dom, 1, 31), "mon": _cron_expand(mon, 1, 12),
            "dow": _cron_expand(dow, 0, 7),
            "dom_star": dom == "*", "dow_star": dow == "*"}


def _cron_fires_on(c: dict, d: datetime.date) -> bool:
    if d.month not in c["mon"]:
        return False
    dow = d.isoweekday() % 7          # Mon=1..Sat=6, Sun=0
    dow_hit = dow in c["dow"] or (dow == 0 and 7 in c["dow"])
    dom_hit = d.day in c["dom"]
    if c["dom_star"] and c["dow_star"]:
        return True
    if c["dom_star"]:
        return dow_hit
    if c["dow_star"]:
        return dom_hit
    return dom_hit or dow_hit          # standard cron OR when both restricted


def _cron_times_local(c: dict, d: datetime.date) -> list[datetime.datetime]:
    """Every fire time on a given UTC calendar day, converted to local time."""
    out = []
    for h, m in product(sorted(c["hour"]), sorted(c["min"])):
        utc = datetime.datetime(d.year, d.month, d.day, h, m,
                                tzinfo=datetime.timezone.utc)
        out.append(utc.astimezone())
    return out


def _humanize_cron(cron: str) -> str:
    c = _parse_cron(cron)
    if not c:
        return cron
    dows = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    parts = []
    if not c["dow_star"]:
        days = sorted({0 if x == 7 else x for x in c["dow"]})
        parts.append(" & ".join(dows[x] for x in days) + " weekly")
    if not c["dom_star"]:
        def ordinal(n):
            return f"{n}{'th' if 11 <= n % 100 <= 13 else {1:'st',2:'nd',3:'rd'}.get(n % 10, 'th')}"
        parts.append(", ".join(ordinal(x) for x in sorted(c["dom"])) + " of the month")
    return "; ".join(parts) if parts else "daily"


# what each workflow does, in the founder's terms. Keyed by a substring of the
# workflow file name; falls back to the workflow's own name.
_WORKFLOW_META = {
    "aeo": {"lane": "aeo", "does": "Full surfacing sweep (~175 questions across 4 engines)"},
    "content": {"lane": "content", "does": "Draft 1 answer page into a pull request"},
}


def _workflow_meta(fname: str) -> dict:
    low = fname.lower()
    for key, meta in _WORKFLOW_META.items():
        if key in low:
            return meta
    return {"lane": "other", "does": ""}


def _workflows() -> dict:
    """Parse the deployed workflows (name + cron) from the default branch."""
    ref, files = _git_workflow_files()
    wfs = []
    for path in files:
        _, text = _git_show(path)
        name = ""
        crons = []
        for line in text.splitlines():
            s = line.strip()
            m = re.match(r"name:\s*(.+)$", s)
            if m and not name:
                name = m.group(1).strip().strip("'\"")
            cm = re.search(r"cron:\s*['\"]([^'\"]+)['\"]", s)
            if cm:
                crons.append(cm.group(1).strip())
        fname = path.split("/")[-1]
        meta = _workflow_meta(fname)
        wfs.append({"file": fname, "name": name or fname, "crons": crons,
                    "lane": meta["lane"], "does": meta["does"],
                    "human": "; ".join(_humanize_cron(c) for c in crons)})
    return {"ref": ref, "workflows": wfs}


def compute_schedule() -> dict:
    def build():
        data = _workflows()
        wfs = data["workflows"]
        now = _local_now()
        today = now.date()

        # this week's Mon..Sun strip, tagging which lanes fire each day
        monday = today - datetime.timedelta(days=today.weekday())
        week = []
        for i in range(7):
            d = monday + datetime.timedelta(days=i)
            lanes = []
            for wf in wfs:
                if any(_cron_fires_on(_parse_cron(c) or {"mon": set()}, d)
                       for c in wf["crons"] if _parse_cron(c)):
                    lanes.append({"lane": wf["lane"], "label": wf["name"]})
            week.append({
                "date": d.isoformat(), "dn": d.strftime("%a"), "num": d.strftime("%d"),
                "today": d == today, "past": d < today, "lanes": lanes})

        # next concrete run datetimes across all workflows, next 24 days
        runs = []
        for wf in wfs:
            for cron in wf["crons"]:
                c = _parse_cron(cron)
                if not c:
                    continue
                for i in range(0, 25):
                    d = today + datetime.timedelta(days=i)
                    if not _cron_fires_on(c, d):
                        continue
                    for dt in _cron_times_local(c, d):
                        if dt <= now:
                            continue
                        days = (dt.date() - today).days
                        rel = ("today" if days == 0 else "tomorrow" if days == 1
                               else f"in {days} days")
                        group = ("Today" if days == 0 else "Tomorrow" if days == 1
                                 else "This week" if days <= (6 - today.weekday())
                                 else "Next 2 weeks")
                        runs.append({
                            "lane": wf["lane"], "label": wf["name"], "does": wf["does"],
                            "date": dt.date().isoformat(), "weekday": dt.strftime("%a"),
                            "time": dt.strftime("%-I:%M %p").lower(),
                            "day_label": dt.strftime("%b %-d"),
                            "rel": rel, "group": group, "sort": dt.isoformat()})
        runs.sort(key=lambda r: r["sort"])
        nxt = runs[0] if runs else None
        return {
            "ref": data["ref"], "today": today.strftime("%a %b %-d"),
            "today_iso": today.isoformat(),
            "workflows": [{"name": w["name"], "lane": w["lane"], "does": w["does"],
                           "human": w["human"], "crons": w["crons"]} for w in wfs],
            "week": week, "next": nxt, "next_runs": runs[:8]}
    return _cached("schedule", 300, build)


# ---------- open PRs (content review queue) ----------
def compute_prs() -> dict:
    def build():
        prs = _gh_json(["pr", "list", "--state", "open", "--limit", "30", "--json",
                        "number,title,headRefName,createdAt,labels,isDraft,url,files,author"])
        if prs is None:
            return {"connected": False, "prs": [],
                    "note": "GitHub CLI not reachable. Run `gh auth login` to light this up."}
        out = []
        for p in prs:
            files = [f.get("path", "") for f in (p.get("files") or [])]
            is_content = (p.get("headRefName", "").startswith("content/")
                          or any("content/answers" in f or f.startswith("content/")
                                 for f in files))
            labels = [l.get("name", "") for l in (p.get("labels") or [])]
            needs_stat = any("stat" in l.lower() for l in labels) or \
                any(k in p.get("title", "").lower() for k in ("needs-stat", "stat-check"))
            out.append({
                "number": p.get("number"), "title": p.get("title", ""),
                "url": p.get("url", ""), "branch": p.get("headRefName", ""),
                "draft": p.get("isDraft", False), "content": is_content,
                "needs_stat": needs_stat, "n_files": len(files),
                "created": (p.get("createdAt") or "")[:10],
                "author": (p.get("author") or {}).get("login", "")})
        out.sort(key=lambda x: (not x["content"], x["created"]))
        return {"connected": True, "prs": out,
                "n_content": sum(1 for p in out if p["content"]),
                "preview_cmd": "bash content/preview.sh",
                "preview_url": "http://localhost:4321/answers"}
    return _cached("prs", 60, build)


# ---------- published content pages ----------
def _frontmatter_title(md: str, fallback: str) -> str:
    m = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', md, re.M)
    return m.group(1).strip() if m else fallback


def compute_content_pages() -> dict:
    pages = []
    if ANSWERS.exists():
        for p in sorted(ANSWERS.glob("*.md")):
            txt = p.read_text(errors="ignore")[:2000]
            pages.append({
                "slug": p.stem,
                "title": _frontmatter_title(txt, p.stem.replace("-", " ")),
                "url": f"https://getclarence.app/answers/{p.stem}"})
    drafted = []
    if CONTENT_WRITTEN.exists():
        try:
            drafted = json.loads(CONTENT_WRITTEN.read_text()).get("queries", [])
        except Exception:
            drafted = []
    return {"n_live": len(pages), "pages": pages,
            "n_drafted": len(drafted), "drafted": drafted}


# ---------- recent activity feed ----------
def _tag_for_conclusion(concl: str) -> str:
    return {"success": "ok", "failure": "err", "cancelled": "warn"}.get(
        (concl or "").lower(), "info")


def compute_activity() -> dict:
    def build():
        events = []
        now = _local_now()

        runs = _gh_json(["run", "list", "--limit", "12", "--json",
                         "name,conclusion,createdAt,event,displayTitle"]) or []
        for r in runs:
            ts = (r.get("createdAt") or "")
            events.append({
                "ts": ts, "tag": _tag_for_conclusion(r.get("conclusion", "")),
                "kind": "ran",
                "text": f'{r.get("name","workflow")} {r.get("conclusion","ran")} '
                        f'({r.get("event","")})'})

        prs = _gh_json(["pr", "list", "--state", "all", "--limit", "15", "--json",
                        "number,title,createdAt,mergedAt,state,url"]) or []
        for p in prs:
            if p.get("mergedAt"):
                events.append({"ts": p["mergedAt"], "tag": "ok", "kind": "merged",
                               "text": f'Merged PR #{p.get("number")}: {p.get("title","")}'})
            if p.get("createdAt"):
                events.append({"ts": p["createdAt"], "tag": "info", "kind": "opened",
                               "text": f'Opened PR #{p.get("number")}: {p.get("title","")}'})

        # AEO sweeps from the ledger (real measured events)
        results = AEO / "results.csv"
        if results.exists():
            rows = list(csv.DictReader(results.open()))
            byrun: dict = defaultdict(lambda: {"provs": set(), "ment": 0})
            for r in rows:
                d = r.get("run_date", "")
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    continue
                byrun[d]["provs"].add(r.get("provider", ""))
                if r.get("mentioned") == "1":
                    byrun[d]["ment"] += 1
            for d in sorted(byrun)[-6:]:
                a = byrun[d]
                events.append({
                    "ts": d + "T16:00:00Z", "tag": "info", "kind": "measured",
                    "text": f'AEO sweep on {len(a["provs"])} engines, '
                            f'{a["ment"]} mentions logged'})

        def keyts(e):
            return e["ts"] or ""
        events.sort(key=keyts, reverse=True)
        seen, dedup = set(), []
        for e in events:
            k = (e["kind"], e["text"])
            if k in seen:
                continue
            seen.add(k)
            # format a friendly local date/time
            iso = e["ts"].replace("Z", "+00:00")
            try:
                dt = datetime.datetime.fromisoformat(iso).astimezone()
                days = (now.date() - dt.date()).days
                when = ("today" if days == 0 else "yesterday" if days == 1
                        else dt.strftime("%b %-d"))
            except Exception:
                when = (e["ts"] or "")[:10]
            dedup.append({"tag": e["tag"], "kind": e["kind"],
                          "when": when, "text": e["text"]})
            if len(dedup) >= 14:
                break
        return {"events": dedup,
                "gh_connected": bool(runs or prs)}
    return _cached("activity", 45, build)


# ---------- waitlist (Cloudflare D1) ----------
def _wrangler_d1(sql: str):
    ok, out = _run(["npx", "wrangler", "d1", "execute", "clarence-waitlist",
                    "--remote", "--json", "--command", sql], SITE, timeout=40)
    if not ok:
        return None
    try:
        data = json.loads(out[out.index("["):])   # skip any wrangler banner text
        return data[0].get("results", [])
    except Exception:
        return None


def compute_waitlist() -> dict:
    def build():
        total = _wrangler_d1("SELECT COUNT(*) AS n FROM waitlist")
        if total is None:
            return {"connected": False,
                    "note": "D1 not reachable (wrangler auth or network). Shows a real "
                            "zero when connected, not a guess."}
        n_total = (total[0].get("n") if total else 0) or 0
        week = _wrangler_d1("SELECT COUNT(*) AS n FROM waitlist "
                            "WHERE created_at >= datetime('now','-7 days')")
        n_week = (week[0].get("n") if week else 0) or 0
        src = _wrangler_d1("SELECT COALESCE(source,'(none)') AS source, COUNT(*) AS n "
                           "FROM waitlist GROUP BY source ORDER BY n DESC") or []
        return {"connected": True, "total": n_total, "this_week": n_week,
                "by_source": [{"source": r.get("source"), "n": r.get("n")} for r in src]}
    return _cached_swr("waitlist", 900, build)


# ---------- signals (the tiles) ----------
def _aeo_surfacing() -> dict:
    """Non-branded vs branded surfacing on the latest full run, plus whether any
    non-branded question has EVER surfaced us across the full runs."""
    results = AEO / "results.csv"
    if not results.exists():
        return {"connected": False}
    rows = list(csv.DictReader(results.open()))
    runs: dict = defaultdict(set)
    for r in rows:
        runs[r["run_date"]].add(r["query"])
    full = sorted([d for d in runs if len(runs[d]) >= 60])
    if not full:
        return {"connected": False}
    latest = full[-1]
    fr = [r for r in rows if r["run_date"] == latest]
    qs = {r["query"] for r in fr}
    nb = {q for q in qs if "clarence" not in q.lower()}
    br = {q for q in qs if "clarence" in q.lower()}
    hit = {r["query"] for r in fr if r.get("mentioned") == "1"}
    nb_hit = len(nb & hit)
    br_hit = len(br & hit)
    # ever surfaced non-branded across all full runs?
    ever = 0
    for d in full:
        drows = [r for r in rows if r["run_date"] == d]
        dhit = {r["query"] for r in drows if r.get("mentioned") == "1"}
        ever += len([q for q in dhit if "clarence" not in q.lower()])
    return {"connected": True, "run": latest, "n_full_runs": len(full),
            "nb_total": len(nb), "nb_hit": nb_hit,
            "br_total": len(br), "br_hit": br_hit, "nb_ever": ever}


def compute_signals() -> dict:
    def build():
        surf = _aeo_surfacing()
        wl = compute_waitlist()
        pages = compute_content_pages()
        traffic = compute_traffic()
        cost = None
        cf = AEO / "cost.json"
        if cf.exists():
            try:
                cost = json.loads(cf.read_text()).get("total")
            except Exception:
                cost = None
        last_day = None
        if not traffic.get("error"):
            s = traffic.get("series") or []
            if s:
                last_day = s[-1]
        return {"surfacing": surf, "waitlist": wl,
                "content": {"n_live": pages["n_live"]},
                "traffic_last": last_day, "cost_total": cost}
    return _cached("signals", 60, build)


# ---------- cost & limits ----------
def compute_cost() -> dict:
    total = None
    runs = []
    cf = AEO / "cost.json"
    if cf.exists():
        try:
            cj = json.loads(cf.read_text())
            total = cj.get("total")
            runs = cj.get("runs", [])
        except Exception:
            pass
    # The Reddit loop bills by the token like everything else, and it runs hourly,
    # so it is the one line here that compounds. Measured, not modeled.
    reddit = None
    try:
        import sys as _sys
        _sys.path.insert(0, str(REDDIT))
        import store as _store
        _con = _store.connect()
        reddit = _store.spend_summary(_con)
        _con.close()
    except Exception as e:
        reddit = {"error": str(e)}
    return {
        "aeo_estimate": total, "aeo_runs": len(runs),
        # cost.json is already newest-first; slicing from the end gave the OLDEST
        # runs and labelled them recent. Sort explicitly rather than trust order.
        "aeo_recent": sorted(runs, key=lambda r: r.get("run", ""), reverse=True)[:6],
        "aeo_schedule": "1st, 11th and 21st of the month",
        "reddit": reddit,
        "meta": {"running": False, "day_cap": 1, "month_cap": 20},
        "not_connected": [
            {"name": "Live Anthropic billing",
             "why": "Connect the usage API for billed dollars and a monthly cap. "
                    "The figure above is modeled from answer length, not a bill."},
            {"name": "GitHub Actions minutes",
             "why": "Read the billing API to show minutes used against the free tier."},
        ],
        "note": "Everything here bills to your Anthropic API key, which is separate "
                "from a Claude Code subscription. Three things spend it: the AEO sweep "
                "(a few times a month, the biggest single charge), the content pipeline "
                "(Tue, Fri, Sat), and the Reddit loop (hourly, small each time but it "
                "compounds). The Reddit figure is measured from real token counts. The "
                "AEO figure is modeled from answer length, not a bill.",
    }


# ---------- ops health ----------
def _age_days(iso_or_date: str) -> float | None:
    try:
        s = iso_or_date.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - dt.astimezone(
            datetime.timezone.utc)).total_seconds() / 86400.0
    except Exception:
        return None


def compute_health() -> dict:
    def build():
        cards = {"Infrastructure": [], "Collectors (read-only)": [], "Lanes": []}

        # --- infra ---
        wl = compute_waitlist()
        cards["Infrastructure"].append({
            "name": "Waitlist store (D1)",
            "status": "ok" if wl.get("connected") else "idle",
            "detail": ("Connected and live. Signup capture responding."
                       if wl.get("connected")
                       else "Not reachable right now. " + wl.get("note", "")),
            "meta": "checked just now"})

        wf = _workflows()
        acts = compute_activity()
        last_run = next((e for e in acts["events"] if e["kind"] == "ran"), None)
        cards["Infrastructure"].append({
            "name": "GitHub Actions",
            "status": "ok" if last_run and last_run["tag"] != "err"
                      else ("err" if last_run else "idle"),
            "detail": f'{len(wf["workflows"])} scheduled workflows. '
                      + (f'Last run: {last_run["text"]}.' if last_run
                         else "No runs seen yet."),
            "meta": f'last run {last_run["when"]}' if last_run else "waiting"})
        cards["Infrastructure"].append({
            "name": "Data spine (D1 marketing)", "status": "idle", "not_built": True,
            "detail": "The self grading warehouse (signals, actions, outcomes) is not "
                      "stood up yet.", "meta": "phase 0"})
        cards["Infrastructure"].append({
            "name": "Cloudflare Workers", "status": "idle", "not_built": True,
            "detail": "Approval inbox and digest Worker not built yet.", "meta": "phase 0"})

        # --- collectors ---
        aeo = compute_aeo()
        aeo_run = aeo.get("run") if not aeo.get("error") else None
        cards["Collectors (read-only)"].append({
            "name": "AEO sweep",
            "status": "ok" if aeo_run else "idle",
            "detail": (f'All engines probed, ledger written ({aeo.get("n_queries",0)} '
                       f'questions).' if aeo_run else "No ledger yet."),
            "meta": f"ran {aeo_run}" if aeo_run else "waiting"})

        qf = REDDIT / "queue.json"
        r_age, r_gen = None, None
        if qf.exists():
            try:
                r_gen = json.loads(qf.read_text()).get("generated")
                r_age = _age_days(r_gen or "")
            except Exception:
                pass
        r_stale = r_age is None or r_age > 3
        cards["Collectors (read-only)"].append({
            "name": "Reddit listener",
            "status": "warn" if r_stale else "ok",
            "label": "stale" if r_stale else "fresh",
            "detail": (f'Queue last refreshed {r_gen[:10] if r_gen else "unknown"}'
                       f'{f", {int(r_age)} days old" if r_age else ""}.'
                       if r_stale else "Queue fresh."),
            "meta": "refresh in Do the work"})

        traffic = compute_traffic()
        t_ok = not traffic.get("error") and (traffic.get("series") or [])
        t_last = t_ok and traffic["series"][-1]["date"]
        t_age = _age_days(t_last) if t_last else None
        cards["Collectors (read-only)"].append({
            "name": "Traffic (Cloudflare)",
            "status": "ok" if (t_age is not None and t_age < 3) else
                      ("warn" if t_ok else "idle"),
            "detail": f"Edge analytics synced through {t_last}." if t_ok
                      else "No traffic rows yet.",
            "meta": "daily"})
        cards["Collectors (read-only)"].append({
            "name": "Ad metrics (Meta)", "status": "idle", "not_built": True,
            "detail": "No active spend. Read-only insights not wired yet.",
            "meta": "paused"})

        # --- lanes ---
        prs = compute_prs()
        n_content_pr = prs.get("n_content", 0)
        cards["Lanes"].append({
            "name": "Content",
            "status": "warn" if n_content_pr else "ok",
            "label": "attention" if n_content_pr else "clear",
            "detail": (f'{n_content_pr} content PR(s) waiting for review.'
                       if n_content_pr
                       else f'Live, no PRs waiting. '
                            f'{compute_content_pages()["n_live"]} page(s) published.'),
            "meta": "Tue, Fri & Sat"})
        cards["Lanes"].append({
            "name": "Engage (Reddit)",
            "status": "warn" if r_stale else "ok",
            "label": "stale" if r_stale else "ok",
            "detail": "Threads authored by hand. "
                      + ("Queue needs a refresh." if r_stale else "Queue fresh."),
            "meta": "manual"})
        cards["Lanes"].append({
            "name": "Site / conversion", "status": "ok",
            "detail": "Live, waitlist capturing"
                      + (f'. {wl.get("total",0)} signup(s) so far.'
                         if wl.get("connected") else "."),
            "meta": "getclarence.app"})
        cards["Lanes"].append({
            "name": "Iterate (brain)", "status": "idle", "not_built": True,
            "detail": "The weekly self review loop is not built yet.", "meta": "phase 4"})

        # overall header status
        allc = [c for g in cards.values() for c in g]
        has_err = any(c["status"] == "err" for c in allc)
        overall = "Needs attention" if has_err else "Operational"
        return {"groups": cards, "overall": overall,
                "next_run": compute_schedule().get("next")}
    return _cached_swr("health", 45, build)


# ---------- dictation ----------
# The same whisper build the Mac app ships, kept warm in a server so a tap on
# the record button comes back in well under a second instead of the twelve it
# takes to load the model per clip. Loopback only, and the audio never leaves
# this machine: there is no transcription service in the loop.
WHISPER_DIR = REPO / "mac" / "resources" / "whisper"
WHISPER_MODEL = WHISPER_DIR / "ggml-small.en-q5_1.bin"
_whisper = {"proc": None, "port": 0}


def _whisper_up() -> int:
    import subprocess
    if _whisper["proc"] and _whisper["proc"].poll() is None:
        return _whisper["port"]
    exe = WHISPER_DIR / "whisper-server"
    if not exe.exists() or not WHISPER_MODEL.exists():
        return 0
    srv = socket.socket(); srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]; srv.close()
    _whisper["proc"] = subprocess.Popen(
        [str(exe), "-m", str(WHISPER_MODEL), "--host", "127.0.0.1",
         "--port", str(port), "-nt"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):                      # the model takes a few seconds
        time.sleep(0.4)
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                _whisper["port"] = port
                return port
        except OSError:
            if _whisper["proc"].poll() is not None:
                return 0
    return 0


def transcribe(raw: bytes, audio_type: str = "") -> dict:
    """Phone recording in, text out. Whatever the browser recorded gets turned
    into the 16kHz mono WAV whisper wants, by ffmpeg, which reads the format
    from the bytes rather than being told."""
    import subprocess
    import tempfile
    import uuid
    if len(raw) < 1200:
        return {"ok": False, "error": "that was too short to hear"}
    port = _whisper_up()
    if not port:
        return {"ok": False, "error": "the local transcriber is not available"}
    tmp = Path(tempfile.gettempdir()) / f"cockpit-{uuid.uuid4().hex}"
    src, wav = tmp.with_suffix(".in"), tmp.with_suffix(".wav")
    try:
        src.write_bytes(raw)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                           capture_output=True, timeout=60)
        if r.returncode != 0 or not wav.exists():
            return {"ok": False, "error": "could not read that recording"}
        boundary = "----cockpit" + uuid.uuid4().hex
        parts = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
        parts += wav.read_bytes()
        parts += (f"\r\n--{boundary}\r\nContent-Disposition: form-data; "
                  f"name=\"response_format\"\r\n\r\ntext\r\n--{boundary}--\r\n").encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/inference", data=parts,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            text = resp.read().decode("utf-8", "replace").strip()
        return {"ok": True, "text": text}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        for f in (src, wav):
            try:
                f.unlink()
            except OSError:
                pass


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        cookie = getattr(self, "_pending_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _authed(self) -> bool:
        """Key in the cookie, or in the link you first opened."""
        if not TOKEN:
            return True
        if f"ck={TOKEN}" in (self.headers.get("Cookie") or ""):
            return True
        return parse_qs(urlparse(self.path).query).get("k", [""])[0] == TOKEN

    def _deny(self):
        self._send(401, "This board needs its key. Open the bookmarked link.",
                   "text/plain")

    def log_message(self, *a):
        pass  # quiet

    def _set_cookie_if_keyed(self):
        if TOKEN and parse_qs(urlparse(self.path).query).get("k", [""])[0] == TOKEN:
            self._pending_cookie = f"ck={TOKEN}; Path=/; Max-Age=31536000; SameSite=Lax"

    def do_GET(self):
        if not self._authed():
            return self._deny()
        self._set_cookie_if_keyed()
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/aeo":
            return self._json(compute_aeo())
        if path == "/api/trend":
            return self._json(compute_trend())
        if path == "/api/runs":
            return self._json(compute_runs())
        if path == "/api/raw":
            q = parse_qs(urlparse(self.path).query)
            return self._json(read_raw(q.get("path", [""])[0]))
        if path == "/api/insights":
            return self._json(compute_insights())
        if path == "/api/traffic":
            return self._json(compute_traffic())
        if path == "/api/sources":
            return self._json(compute_sources())
        if path == "/api/voices":
            return self._json(compute_voices())
        if path == "/api/authors":
            return self._json(compute_authors())
        if path == "/api/aeo/pages":
            q = parse_qs(urlparse(self.path).query)
            return self._json(top_pages_for(q.get("domain", ["reddit.com"])[0]))
        if path == "/api/reddit":
            q = parse_qs(urlparse(self.path).query)
            try:
                depth = max(0.01, min(1.0, float(q.get("depth", ["0.10"])[0])))
            except ValueError:
                depth = 0.10
            try:
                within = float(q.get("within", ["0"])[0])     # hours, 0 = no limit
            except ValueError:
                within = 0.0
            order = q.get("order", ["best"])[0]
            if q.get("done", ["0"])[0] == "1":
                return self._json(reddit_done())
            return self._json({**reddit_queue(depth, within, order),
                               "health": reddit_health(),
                               "voice": voice_stats(),
                               "power": machine_power()})
        if path == "/api/reddit/shortlist":
            # The day's list rather than the whole queue: see shortlist.py for
            # why the judge's score alone is not the ordering you want.
            q = parse_qs(urlparse(self.path).query)
            try:
                want = max(1, min(60, int(q.get("want", ["20"])[0])))
                hours = max(6, min(720, int(q.get("hours", ["168"])[0])))
            except ValueError:
                want, hours = 20, 168
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                import shortlist as _sl
                _con = _store.connect()
                out = _sl.build(_con, want=want, floor=10, hours=hours)
                out["new_communities"] = _sl.new_communities(_con)
                out["asks"] = _store.ask_stats(_con)
                _asked = _store.asked_authors(_con)
                state = load_state()
                for t in out["picks"]:
                    st = state.get(t["id"], {})
                    t["draft"] = st.get("draft", "")
                    t["post_text"] = st.get("post_text", "")
                    t["angle"] = st.get("angle", "")
                    t["comments"] = st.get("comments", [])
                    t["fit"] = t.get("rank", 0)
                    a = _asked.get((t.get("author") or "").lower())
                    t["asked_at"] = a["at"] if a else None
                _con.close()
                return self._json({**out, "health": reddit_health(),
                                   "replies": local_replies(),
                                   **board_can()})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/reddit/search":
            q = (parse_qs(urlparse(self.path).query).get("q", [""])[0] or "").strip()
            if not q:
                return self._json({"q": "", "posts": [], "people": []})
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                _con = _store.connect()
                out = _store.search(_con, q)
                _con.close()
                return self._json(out)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/reddit/people":
            pid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                _con = _store.connect()
                rows = (_store.people_for(_con, pid) if pid
                        else _store.saved_people(_con))
                _con.close()
                return self._json({"count": len(rows), "people": rows})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/reddit/prospects":
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                _con = _store.connect()
                rows = _store.prospects(_con)
                _con.close()
                return self._json({"count": len(rows), "people": rows})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/reddit/skips":
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                _con = _store.connect()
                out = _store.skip_report(_con)
                _con.close()
                return self._json(out)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/reddit/lanes":
            # The lanes themselves, plus the places you are allowed to post.
            if reddit_monitor is None:
                return self._json({"error": "monitor.py not importable"}, 500)
            try:
                out = {}
                for lane in reddit_monitor.all_lanes():
                    w = reddit_monitor.load_watch(lane)
                    out[lane] = {
                        "what": w.get("_what", ""),
                        "n_subs": len(w.get("native_subs", [])) + len(w.get("subreddits", [])),
                        "post_here": w.get("post_here", []),
                    }
                return self._json({"lanes": out})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/reddit/find":
            # "Who is talking about this, and where?" Answered from real posts.
            q = (parse_qs(urlparse(self.path).query).get("q", [""])[0] or "").strip()
            lane = parse_qs(urlparse(self.path).query).get("lane", ["conversations"])[0]
            if reddit_monitor is None:
                return self._json({"error": "monitor.py not importable"}, 500)
            if not q:
                return self._json({"error": "ask a question with ?q="}, 400)
            try:
                reddit_monitor.load_env()
                return self._json(reddit_monitor.find_subs(q, lane=lane))
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/reddit/subs":
            # Which communities actually pay off, from the triage record.
            if reddit_monitor is None:
                return self._json({"error": "monitor.py not importable"}, 500)
            try:
                lane = parse_qs(urlparse(self.path).query).get("lane", ["conversations"])[0]
                return self._json(reddit_monitor.subs_report(lane=lane))
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/corpus":
            return self._json(corpus_view())
        if path == "/api/content/plan":
            return self._json(compute_content_plan())
        if path == "/api/health":
            return self._json(compute_health())
        if path == "/api/schedule":
            return self._json(compute_schedule())
        if path == "/api/prs":
            return self._json(compute_prs())
        if path == "/api/ideas":
            return self._json(compute_ideas())
        if path == "/api/outreach":
            mod = _outreach()
            if not mod:
                return self._json({"error": "outreach.py not importable"}, 500)
            return self._json(mod.view())
        if path == "/api/content/pages":
            return self._json(compute_content_pages())
        if path == "/api/activity":
            return self._json(compute_activity())
        if path == "/api/waitlist":
            return self._json(compute_waitlist())
        if path == "/api/signals":
            return self._json(compute_signals())
        if path == "/api/cost":
            return self._json(compute_cost())
        if path in ("/m", "/m/", "/mobile"):
            f = M_HTML if M_HTML.exists() else WEB / "m.html"
            if f.exists():
                self._set_cookie_if_keyed()
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
        if path.startswith("/web/"):
            # Resolve, then check it is genuinely INSIDE web/. Without this,
            # GET /web/../../reddit/reddit.db served the whole database: pathlib
            # will happily join its way out of the directory you meant.
            try:
                f = (WEB / path[len("/web/"):]).resolve()
                f.relative_to(WEB.resolve())
            except (ValueError, OSError):
                return self._json({"error": "not found"}, 404)
            if f.suffix not in (".css", ".js", ".html"):
                return self._json({"error": "not found"}, 404)
            if f.exists() and f.is_file():
                # The allowlist admits .html, so the type map has to as well.
                # Serving a page as application/javascript renders it as source.
                ctype = {".css": "text/css",
                         ".html": "text/html; charset=utf-8"}.get(
                             f.suffix, "application/javascript")
                return self._send(200, f.read_bytes(), ctype)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authed():
            return self._deny()
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        if path == "/api/reddit/transcribe":
            # The body is audio, not JSON, so this branch comes first.
            raw = self.rfile.read(length) if length else b""
            return self._json(transcribe(raw, self.headers.get("X-Audio-Type", "")))
        body = json.loads(self.rfile.read(length) or "{}") if length else {}
        if path == "/api/sources/save":
            url = body.get("url")
            if not url:
                return self._json({"error": "missing url"}, 400)
            save_source(url, body.get("saved"), body.get("note"))
            return self._json({"ok": True})
        if path == "/api/reddit/item":
            pid = body.get("id")
            if not pid:
                return self._json({"error": "missing id"}, 400)
            if "status" in body:
                # Your decisions are the half of the loop nothing else can supply,
                # so they go into the record, not just this session's state file.
                try:
                    import sys as _sys
                    _sys.path.insert(0, str(REDDIT))
                    import store as _store
                    _con = _store.connect()
                    _store.set_status(_con, pid, body["status"],
                                      body.get("reason"), body.get("note"))
                    _store.log_event(_con, pid, "status", {
                        "status": body["status"],
                        "reason": body.get("reason"),
                        "note": body.get("note")})
                    _con.close()
                except Exception as e:
                    print(f"  ! could not record status in the store: {e}")
            for k in [k for k in _cache if k.startswith("reddit_queue:")]:
                _cache.pop(k, None)          # your click must be visible at once
            with _STATE_LOCK:
                state = load_state()
                entry = state.get(pid, {})
                if "status" in body:
                    entry["status"] = body["status"]
                # The three boxes of the reply card: the post you pasted, the
                # angle you dictated, the draft you got back. Any subset.
                for field in ("draft", "post_text", "angle"):
                    if field in body:
                        entry[field] = body[field]
                state[pid] = entry
                save_state(state)
                # Read inside the lock, used outside it: what the draft box holds
                # once this save has landed.
                final_text = entry.get("draft") or ""
            # The words, now that the state file agrees with the screen.
            #
            # `state.json` keeps exactly one draft per thread and overwrites it,
            # so by itself it cannot answer the only question that matters later:
            # was this the model's sentence or his. Here each save becomes its own
            # row, which makes the before and after a fact rather than a guess.
            if body.get("status") == "posted":
                # Forced, because a good draft sometimes goes out untouched and
                # that is still a reply that was sent.
                capture_step(pid, "final", final_text, force=True)
            elif "draft" in body:
                capture_step(pid, "edit", body.get("draft"))
            if "angle" in body:
                capture_step(pid, "angle", body.get("angle"))
            return self._json({"ok": True})
        if path == "/api/reddit/watch":
            # Add or drop a subreddit without hand-editing watch.json.
            sub = (body.get("sub") or "").strip().lstrip("/").removeprefix("r/")
            action = body.get("action", "add")
            if not sub or action not in ("add", "drop"):
                return self._json({"ok": False, "error": "need sub + add|drop"}, 400)
            wfile = REDDIT / "watch.json"
            try:
                w = json.loads(wfile.read_text())
                lane = body.get("lane", "conversations")
                target = (w.get("lanes") or {}).get(lane, w)
                wide, native = target.get("subreddits", []), target.get("native_subs", [])
                low = sub.lower()
                if action == "add":
                    if low not in {x.lower() for x in wide + native}:
                        wide.append(sub)
                else:
                    wide = [x for x in wide if x.lower() != low]
                    native = [x for x in native if x.lower() != low]
                target["subreddits"], target["native_subs"] = wide, native
                wfile.write_text(json.dumps(w, indent=2) + "\n")
                return self._json({"ok": True, "watching": len(wide) + len(native)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/reddit/enrich":
            if reddit_enrich is None:
                return self._json({"ok": False, "error": "enrich.py not importable"}, 500)
            pid = body.get("id")
            url = body.get("url")
            if not url and pid:
                for t in reddit_queue().get("threads", []):
                    if t.get("id") == pid:
                        url = t.get("url")
                        break
            if not url and not (body.get("post_text") or "").strip():
                return self._json(
                    {"ok": False,
                     "error": "no url for that thread, and no post pasted"}, 400)
            try:
                now = datetime.datetime.now().isoformat(timespec="seconds")
                out = reddit_enrich.enrich(url or "", when=now,
                                           post_text=body.get("post_text"))
                # The suggested reply comes back with the analysis and sits there
                # as a reference. It no longer writes itself into your draft box:
                # the draft is what YOU asked for, with your own angle in it.
                return self._json({"ok": True, **out})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path in ("/api/reddit/pull-people", "/api/reddit/enrich",
                    "/api/reddit/draft") and body.get("url"):
            if not _reddit_url_ok(body.get("url")):
                return self._json(
                    {"ok": False,
                     "error": "that url is not a Reddit link, so it will not be fetched"},
                    400)
        if path == "/api/reddit/pull-people":
            # Go and read who is actually in the thread, and what they said.
            if reddit_enrich is None:
                return self._json({"ok": False, "error": "enrich.py not importable"}, 500)
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                found = reddit_enrich.scrape_people(body.get("url") or "")
                _con = _store.connect()
                added = _store.add_people(_con, body.get("id") or "",
                                          body.get("subreddit") or "", found)
                rows = _store.people_for(_con, body.get("id") or "")
                _con.close()
                return self._json({"ok": True, "added": added, "people": rows})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/reddit/person":
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                _con = _store.connect()
                _store.save_person(_con, body.get("author"), body.get("post_id"),
                                   bool(body.get("on")), body.get("note"))
                n = len(_store.saved_people(_con))
                _con.close()
                return self._json({"ok": True, "count": n})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/reddit/event":
            note_event(body.get("id"), (body.get("kind") or "note")[:40],
                       body.get("detail") if isinstance(body.get("detail"), dict) else None)
            return self._json({"ok": True})
        if path == "/api/reddit/full":
            # The whole post and its comments for ONE thread, on demand, so the
            # board can show what the person actually wrote instead of the
            # 281-character listing preview. One thread per tap: the feed this
            # reads is rate limited, so it must never be used to sweep a queue.
            if reddit_enrich is None:
                return self._json({"ok": False, "error": "enrich.py not importable"}, 500)
            pid, url = body.get("id"), body.get("url")
            if not url and pid:
                for t in reddit_queue().get("threads", []):
                    if t.get("id") == pid:
                        url = t.get("url")
                        break
            if not url:
                return self._json({"ok": False, "error": "no url for that thread"}, 400)
            try:
                t = reddit_enrich.scrape_thread(url)
            except reddit_enrich.RateLimited as e:
                # Not an error the user caused, and it clears on its own.
                return self._json({"ok": False, "retry": True, "error": str(e)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            text = (t.get("selftext") or "").strip()
            if pid and text:
                # Fetched once, kept. The draft path reads the same field.
                with _STATE_LOCK:
                    state = load_state()
                    entry = state.get(pid, {})
                    entry["post_text"] = text
                    entry["comments"] = (t.get("comments") or [])[:25]
                    state[pid] = entry
                    save_state(state)
                for k in [k for k in _cache if k.startswith("reddit_queue:")]:
                    _cache.pop(k, None)
            note_event(pid, "full", {"via": t.get("via", ""), "chars": len(text),
                                     "comments": len(t.get("comments") or [])})
            return self._json({"ok": True, "post_text": text,
                               "title": t.get("title", ""),
                               "comments": t.get("comments", [])[:25],
                               "n_comments": t.get("n_comments", 0),
                               "via": t.get("via", "")})
        if path == "/api/reddit/ask":
            # Draft the ask. Drafting records nothing: a message you look at and
            # discard must not count as having asked someone.
            if reddit_enrich is None:
                return self._json({"ok": False, "error": "enrich.py not importable"}, 500)
            try:
                out = reddit_enrich.draft_ask(
                    body.get("author") or "",
                    body.get("their_words") or "",
                    body.get("my_comment") or "",
                    channel=body.get("channel", "thread"))
                return self._json(out)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/reddit/ask-sent":
            # You actually sent it. Recorded against the PERSON so they never
            # get asked twice, whichever thread they turn up in next.
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                _con = _store.connect()
                _store.record_ask(_con, body.get("author") or "",
                                  post_id=body.get("id"),
                                  reply_id=body.get("reply_id"),
                                  channel=body.get("channel", "thread"),
                                  text=body.get("text"))
                _store.log_event(_con, body.get("id"), "ask",
                                 {"author": body.get("author"),
                                  "channel": body.get("channel", "thread")})
                stats = _store.ask_stats(_con)
                _con.close()
                for k in [k for k in _cache if k.startswith("reddit_queue:")]:
                    _cache.pop(k, None)
                return self._json({"ok": True, "stats": stats})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/reddit/ask-outcome":
            # What came back. The only measurement of whether any of this works.
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                _con = _store.connect()
                _store.set_ask_outcome(_con, body.get("author") or "",
                                       body.get("outcome") or "sent")
                stats = _store.ask_stats(_con)
                _con.close()
                return self._json({"ok": True, "stats": stats})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/reddit/reply-ack":
            # "I have dealt with this one." Keeps the reply inbox a list of things
            # still owed an answer rather than an ever-growing log.
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                _con = _store.connect()
                _store.ack_reply(_con, body.get("id"), bool(body.get("on", True)))
                _con.close()
                for k in [k for k in _cache if k.startswith("reddit_queue:")]:
                    _cache.pop(k, None)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/reddit/prospect":
            try:
                import sys as _sys
                _sys.path.insert(0, str(REDDIT))
                import store as _store
                _con = _store.connect()
                _store.set_prospect(_con, body.get("id"), bool(body.get("on")),
                                    body.get("note"))
                _store.log_event(_con, body.get("id"), "prospect",
                                 {"on": bool(body.get("on")),
                                  "note": body.get("note")})
                n = len(_store.prospects(_con))
                _con.close()
                for k in [k for k in _cache if k.startswith("reddit_queue:")]:
                    _cache.pop(k, None)
                return self._json({"ok": True, "count": n})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/reddit/revise":
            if reddit_enrich is None:
                return self._json({"ok": False, "error": "enrich.py not importable"}, 500)
            draft = (body.get("draft") or "").strip()
            what = (body.get("instruction") or "").strip()
            if not draft:
                return self._json({"ok": False, "error": "nothing to revise yet"}, 400)
            if not what:
                return self._json({"ok": False, "error": "say what to change"}, 400)
            note_event(body.get("id"), "revise", {"instruction": what[:200]})
            try:
                out = reddit_enrich.revise_draft(
                    draft, what,
                    post_text=body.get("post_text"), angle=body.get("angle"),
                    author_id=body.get("author", "curious-newcomer"))
                capture_step(body.get("id"), "revise", out.get("draft"),
                             author=body.get("author", "curious-newcomer"),
                             instruction=what[:200])
                return self._json({"ok": True, **out})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/reddit/draft":
            # Draft a reply AS a chosen author persona, for a chosen reply context
            # (new top-level comment / reply to a specific comment / seed a post).
            if reddit_enrich is None:
                return self._json({"ok": False, "error": "enrich.py not importable"}, 500)
            pid, url = body.get("id"), body.get("url")
            if not url and pid:
                for t in reddit_queue().get("threads", []):
                    if t.get("id") == pid:
                        url = t.get("url")
                        break
            if not url and not (body.get("post_text") or "").strip():
                return self._json(
                    {"ok": False,
                     "error": "no url for that thread, and no post pasted"}, 400)
            try:
                out = reddit_enrich.draft_reply(
                    url or "",
                    author_id=body.get("author", "curious-newcomer"),
                    context=body.get("context", "new_comment"),
                    target_comment=body.get("target_comment"),
                    angle=body.get("angle"),
                    post_text=body.get("post_text"),
                    my_comment=body.get("my_comment"))
                author = body.get("author", "curious-newcomer")
                context = body.get("context", "new_comment")
                note_event(pid, "draft", {
                    "author": author, "context": context,
                    "from_angle": bool((body.get("angle") or "").strip()),
                    "angle_chars": len((body.get("angle") or "").strip()),
                    "draft_chars": len(out.get("draft") or "")})
                # What he dictated is already his voice, rough and spoken. What
                # came back is the model's. Both are kept, and kept apart.
                capture_step(pid, "angle", body.get("angle"),
                             author=author, context=context)
                capture_step(pid, "model", out.get("draft"),
                             author=author, context=context)
                return self._json({"ok": True, **out})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/content/draft":
            return self._json(draft_guide((body or {}).get("query", "")))
        if path == "/api/ideas/seed":
            mod = _backlog()
            if not mod:
                return self._json({"ok": False, "error": "ideas.py not importable"}, 500)
            try:
                return self._json({"ok": True, **mod.seed()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if path == "/api/ideas/add":
            mod = _backlog()
            if not mod:
                return self._json({"ok": False, "error": "ideas.py not importable"}, 500)
            title = (body.get("title") or "").strip()
            if not title:
                return self._json({"ok": False, "error": "missing title"}, 400)
            idea = mod.add(title, angle=body.get("angle", ""),
                           kind=body.get("kind", "guide"), source="manual")
            return self._json({"ok": bool(idea), "idea": idea,
                               "error": None if idea else "duplicate or empty title"})
        if path == "/api/ideas/update":
            mod = _backlog()
            if not mod:
                return self._json({"ok": False, "error": "ideas.py not importable"}, 500)
            iid = body.get("id")
            if not iid:
                return self._json({"ok": False, "error": "missing id"}, 400)
            fields = {k: body[k] for k in ("status", "angle", "notes", "kind",
                                           "title", "pr_url", "published_url")
                      if k in body}
            idea = mod.update(iid, **fields)
            return self._json({"ok": bool(idea), "idea": idea})
        if path == "/api/ideas/delete":
            mod = _backlog()
            if not mod:
                return self._json({"ok": False, "error": "ideas.py not importable"}, 500)
            iid = body.get("id")
            if not iid:
                return self._json({"ok": False, "error": "missing id"}, 400)
            return self._json({"ok": mod.remove(iid)})
        if path == "/api/ideas/draft":
            iid = body.get("id")
            if not iid:
                return self._json({"ok": False, "error": "missing id"}, 400)
            return self._json(draft_longform(iid))
        if path == "/api/outreach/add":
            mod = _outreach()
            if not mod:
                return self._json({"ok": False, "error": "outreach.py not importable"}, 500)
            url = (body.get("url") or "").strip()
            handle = (body.get("handle") or "").strip()
            if not url and not handle:
                return self._json({"ok": False, "error": "need a url or a handle"}, 400)
            entry = mod.add(url=url, handle=handle,
                            signal=body.get("signal", ""),
                            action=body.get("action", ""),
                            message=body.get("message", ""),
                            warm=bool(body.get("warm", False)),
                            notes=body.get("notes", ""))
            return self._json({"ok": True, "entry": entry})
        if path == "/api/outreach/update":
            mod = _outreach()
            if not mod:
                return self._json({"ok": False, "error": "outreach.py not importable"}, 500)
            eid = body.get("id")
            if not eid:
                return self._json({"ok": False, "error": "missing id"}, 400)
            fields = {k: body[k] for k in ("status", "handle", "signal", "action",
                                           "message", "notes", "warm", "url")
                      if k in body}
            entry = mod.update(eid, **fields)
            return self._json({"ok": bool(entry), "entry": entry})
        if path == "/api/outreach/delete":
            mod = _outreach()
            if not mod:
                return self._json({"ok": False, "error": "outreach.py not importable"}, 500)
            eid = body.get("id")
            if not eid:
                return self._json({"ok": False, "error": "missing id"}, 400)
            return self._json({"ok": mod.remove(eid)})
        if path == "/api/reddit/refresh":
            try:
                # Searching + the triage pass takes a minute or two, so give it room.
                subprocess.run([sys.executable, str(REDDIT / "monitor.py")],
                               check=True, capture_output=True, timeout=420)
                # archived.py used to run here, tagging each queued thread
                # archived or commentable so the reply queue only served ones you
                # can post to. Taken out 2026-10-18, because it cannot answer the
                # question any more and was answering it wrongly: logged-out
                # per-thread reads have been walled since 2026-10-17, old.reddit
                # hands back its login page with a 200, and that page carries
                # neither archived marker, so every thread came back "commentable"
                # including one made up to test it. It is honest now (unknown, and
                # it writes nothing), which makes it 21 requests per Refresh that
                # can only say "walled".
                #
                # Nothing on the board loses a state. `archived` only ever reached
                # compute_content_plan below and the queue.json fallback: the reply
                # queue and the Voice lab read _build_reddit_queue, which comes off
                # the store, and `posts` had no such column. It has both now,
                # added the same day and filled from the OAuth listing the
                # collector already reads: `monitor._from_api` sees `archived`
                # and `locked` on every post, for free, and keeps them, so the
                # split reaches the board and the phone, which this never did.
                return self._json({"ok": True, "queue": reddit_queue()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)


def main() -> int:
    # Warm the panels that ask the network, so the first person to open the board
    # is not the one who pays for it. Three wrangler calls take about nine seconds
    # and the header blocks on them.
    def warm():
        for fn in (compute_waitlist, compute_health):
            try:
                fn()
            except Exception:
                pass

    threading.Thread(target=warm, daemon=True).start()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    if HOST not in ("127.0.0.1", "localhost"):
        import socket
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))            # no packet sent, just asks
            lan = probe.getsockname()[0]              # the OS which address it uses
            probe.close()
        except Exception:
            lan = HOST
        print(f"Clarence cockpit → http://{lan}:{PORT}  (open this from your other Mac)")
        print(f"  Key required. Bookmark this exact link:\n    http://{lan}:{PORT}/?k={TOKEN}")
    elif TOKEN:
        print(f"Clarence cockpit → http://localhost:{PORT}  (Ctrl-C to stop)")
        print(f"  Key required, because COCKPIT_REQUIRE_KEY=1. Behind a tunnel,")
        print(f"  open the tunnel's own hostname with:\n    /?k={TOKEN}")
    else:
        print(f"Clarence cockpit → http://localhost:{PORT}  (Ctrl-C to stop)")
    threading.Thread(target=_whisper_up, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
