#!/usr/bin/env python3
"""The AEO sources repository: every page an answer engine has cited, ever.

The probe saves each raw answer. Individually they are noise; aggregated they
are the map of who wins our query universe and where to show up. This walks
EVERY raw answer across ALL runs, pulls every external link, and builds one
durable catalog (sources.json): per URL, which of our questions cite it, how
many times, on which engines, first and last seen. It accumulates over runs, so
a page cited once months ago is still on record.

Rebuildable and idempotent. Reads results.csv for the query<->answer mapping.
A separate saved.json (managed by the cockpit) holds the founder's pins/notes
and is never touched here.

    python3 sources_repo.py            # rebuild sources.json from all runs
    python3 sources_repo.py --top 30   # also print the top sources
"""
from __future__ import annotations

import csv, json, os, re, sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "results.csv"
OUT = HERE / "sources.json"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_RE = re.compile(r"https?://[^\s\"'<>)\]}]+")

# Search-engine grounding wrappers / redirects, not real content sources.
NOISE = ("vertexaisearch.cloud.google.com", "googleusercontent.com",
         "grounding-api-redirect", "google.com/search", "bing.com",
         "duckduckgo.com", "gstatic.com")

PLATFORM = [
    ("reddit.com", "reddit"), ("youtube.com", "youtube"), ("youtu.be", "youtube"),
    ("facebook.com", "facebook"), ("apps.apple.com", "app store"),
    ("play.google.com", "play store"), ("instagram.com", "instagram"),
    ("tiktok.com", "tiktok"), ("twitter.com", "x"), ("x.com", "x"),
    ("medium.com", "blog"), ("substack.com", "blog"), ("dev.to", "blog"),
    ("quora.com", "quora"), ("producthunt.com", "product hunt"),
    ("alternativeto.net", "alternativeto"), ("github.com", "github"),
]


def host(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def platform_of(h: str) -> str:
    for frag, name in PLATFORM:
        if frag in h:
            return name
    return "web"


def is_noise(url: str) -> bool:
    return any(n in url for n in NOISE)


def normalize(url: str) -> str:
    url = url.rstrip(".,);]}'\"")
    p = urlparse(url)
    path = p.path.rstrip("/")
    return f"{p.scheme}://{host(url)}{path}"


def title_of(url: str, h: str) -> str:
    """A readable label from the URL slug (no network)."""
    if "reddit.com" in h:
        m = re.search(r"/comments/[a-z0-9]+/([a-z0-9_]+)", url)
        if m:
            return m.group(1).replace("_", " ").strip().capitalize()
    seg = [s for s in urlparse(url).path.split("/") if s]
    # skip a trailing all-numeric id (facebook post ids, etc.); use the slug
    while seg and re.fullmatch(r"\d+", seg[-1]):
        seg.pop()
    if seg:
        last = re.sub(r"\.(html?|php|aspx?)$", "", seg[-1])
        last = re.sub(r"[-_]+", " ", last).strip()
        if len(last) > 3:
            return last[:90].capitalize()
    return h


def build(top: int = 0) -> dict:
    rows = [r for r in csv.DictReader(LEDGER.open(newline=""))
            if DATE_RE.match((r.get("run_date") or "").strip())]
    agg: dict[str, dict] = {}
    for r in rows:
        rp = (r.get("raw_path") or "").strip()
        if not rp:
            continue
        f = HERE / rp
        if not f.exists():
            continue
        text = f.read_text(errors="ignore")
        query, run, prov = r["query"], r["run_date"], r["provider"]
        seen_here = set()
        for raw in URL_RE.findall(text):
            if is_noise(raw):
                continue
            url = normalize(raw)
            if not url or "://" not in url:
                continue
            if url in seen_here:
                continue          # count a URL once per answer
            seen_here.add(url)
            h = host(url)
            e = agg.get(url)
            if not e:
                e = agg[url] = {"url": url, "domain": h, "platform": platform_of(h),
                                "title": title_of(url, h), "queries": set(),
                                "providers": set(), "runs": set(), "answers": 0}
            e["queries"].add(query)
            e["providers"].add(prov)
            e["runs"].add(run)
            e["answers"] += 1

    out = []
    for e in agg.values():
        runs = sorted(e["runs"])
        out.append({
            "url": e["url"], "domain": e["domain"], "platform": e["platform"],
            "title": e["title"], "n_queries": len(e["queries"]),
            "answers": e["answers"], "queries": sorted(e["queries"]),
            "providers": sorted(e["providers"]),
            "first_seen": runs[0], "last_seen": runs[-1],
        })
    out.sort(key=lambda s: (-s["n_queries"], -s["answers"]))
    repo = {"built_from_runs": sorted({r["run_date"] for r in rows}),
            "n_sources": len(out), "sources": out}
    OUT.write_text(json.dumps(repo, indent=2))
    print(f"wrote {OUT.name}: {len(out)} sources across {len(repo['built_from_runs'])} runs")
    if top:
        print(f"\nTop {top} cited sources:")
        for s in out[:top]:
            print(f"  {s['n_queries']:2}q {s['answers']:3}x  [{s['platform']:10}] {s['title'][:50]}")
    return repo


def main() -> int:
    top = 0
    if "--top" in sys.argv:
        top = int(sys.argv[sys.argv.index("--top") + 1])
    build(top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
