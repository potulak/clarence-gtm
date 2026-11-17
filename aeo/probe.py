#!/usr/bin/env python3
"""Clarence AEO probe — the measurement step of docs/gtm/aeo-geo-playbook.md.

For every query in queries.txt, ask each configured answer engine the question
exactly as a user would, then record whether Clarence is mentioned, where, and
whether one of our URLs is cited. One row per query x provider is appended to
results.csv; the full raw answer is kept under raw/<run-date>/<provider>/ so
that for queries we don't win we can study what *does* win and match its shape.

Designed for a monthly run (AEO compounds slowly; diff month over month):

    python3 scratch/aeo/probe.py            # full run, all configured providers
    python3 scratch/aeo/probe.py --check    # show which providers have keys
    python3 scratch/aeo/probe.py --limit 3 --providers perplexity   # smoke test

Cron suggestion (1st of the month, 9am):
    0 9 1 * * cd <repo> && python3 scratch/aeo/probe.py >> scratch/aeo/probe.log 2>&1

Keys are read from the environment or from scratch/aeo/.env (KEY=value lines,
gitignored). Providers with no key are skipped and noted — never faked:

    OPENAI_API_KEY      · model override OPENAI_MODEL      (default gpt-4o)
    ANTHROPIC_API_KEY   · model override ANTHROPIC_MODEL   (default claude-sonnet-5)
    PERPLEXITY_API_KEY  · model override PERPLEXITY_MODEL  (default sonar)
    GEMINI_API_KEY      · model override GEMINI_MODEL      (default gemini-2.5-flash)

OpenAI, Anthropic and Gemini are called with their web-search tool enabled so
they behave like the answer engines users actually face; Perplexity is one
natively. Stdlib only — no pip installs needed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUERIES_FILE = HERE / "queries.txt"
RESULTS_FILE = HERE / "results.csv"
RAW_DIR = HERE / "raw"

# Self-heal SSL: some Python builds (e.g. Homebrew python@3.14 on macOS) ship
# with an empty default trust store, so HTTPS to certain hosts (Perplexity hit
# this) fails with CERTIFICATE_VERIFY_FAILED while others pass by luck. If
# SSL_CERT_FILE isn't already set, point urllib at the first CA bundle we find.
# Keeps the hands-off run from silently losing a provider to a cert error.
if not os.environ.get("SSL_CERT_FILE"):
    for _ca in ("/etc/ssl/cert.pem",
                "/opt/homebrew/etc/openssl@3/cert.pem",
                "/opt/homebrew/etc/ca-certificates/cert.pem"):
        if os.path.exists(_ca):
            os.environ["SSL_CERT_FILE"] = _ca
            break

TIMEOUT = 120
RETRIES = 3
BACKOFF = (3, 10, 25)  # seconds before retry 1, 2, 3

# Domains that count as "our URL was cited". Keep in sync with site/src/config.ts
# SITE_URL.
CLARENCE_DOMAINS = [
    d.strip().lower()
    for d in os.environ.get("CLARENCE_DOMAINS", "getclarence.app").split(",")
    if d.strip()
]

MENTION_RE = re.compile(r"\bclarence\b", re.IGNORECASE)
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")

CSV_FIELDS = [
    "run_date",
    "provider",
    "model",
    "cluster",
    "query",
    "ok",            # 1 = call succeeded, 0 = error (see error column)
    "mentioned",     # 1 = the answer text mentions Clarence
    "position",      # list rank of the first mentioning list item; 0 = mentioned in prose; blank = not mentioned
    "clarence_cited",  # 1 = a CLARENCE_DOMAINS url appears in the citations
    "clarence_url",
    "cited_domains",  # every domain the answer cited, semicolon-joined — the "who wins instead" signal
    "answer_chars",
    "raw_path",
    "error",
]


# ---------------------------------------------------------------- env / setup

def load_dotenv(path: Path) -> None:
    """Tiny .env loader — KEY=value lines, '#' comments, no override of real env."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


def read_queries(path: Path) -> list[tuple[str, str, str]]:
    """Return (tier, cluster, query) triples. '## cluster: name' headers set the
    cluster; '## tier: name' headers set the tier (default 'core'); any query that
    names Clarence is tier 'branded' regardless of section."""
    out: list[tuple[str, str, str]] = []
    cluster = ""
    tier = "core"
    for line in path.read_text().splitlines():
        line = line.strip()
        mc = re.match(r"^##\s*cluster:\s*(.+)$", line, re.IGNORECASE)
        if mc:
            cluster = mc.group(1).strip()
            continue
        mt = re.match(r"^##\s*tier:\s*(\w+)", line, re.IGNORECASE)
        if mt:
            tier = mt.group(1).lower()
            continue
        if not line or line.startswith("#"):
            continue
        t = "branded" if "clarence" in line.lower() else tier
        out.append((t, cluster, line))
    return out


def _prior_run_count() -> int:
    """Distinct run_dates already in the ledger, used to alternate the branded tier."""
    if not RESULTS_FILE.exists():
        return 0
    seen = set()
    for r in csv.DictReader(RESULTS_FILE.open(newline="")):
        rd = (r.get("run_date") or "").strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", rd):
            seen.add(rd)
    return len(seen)


def http_json(url: str, payload: dict, headers: dict) -> dict:
    """POST JSON with retries on 429/5xx; raise on final failure."""
    body = json.dumps(payload).encode()
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json", **headers}
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            last = RuntimeError(f"HTTP {e.code}: {detail}")
            if e.code in (429, 500, 502, 503, 529) and attempt < RETRIES:
                time.sleep(BACKOFF[attempt])
                continue
            raise last from None
        except Exception as e:  # timeouts, connection resets
            last = e
            if attempt < RETRIES:
                time.sleep(BACKOFF[attempt])
                continue
            raise
    raise last  # unreachable


# ------------------------------------------------------------------ providers
# Each returns (answer_text, citation_urls). Citations are the URLs the answer
# itself points at (not every page the engine consulted), where the API
# distinguishes the two.

def ask_openai(query: str, model: str, key: str) -> tuple[str, list[str]]:
    data = http_json(
        "https://api.openai.com/v1/responses",
        {"model": model, "input": query, "tools": [{"type": "web_search"}]},
        {"Authorization": f"Bearer {key}"},
    )
    texts, urls = [], []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                texts.append(part.get("text", ""))
                for ann in part.get("annotations", []):
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        urls.append(ann["url"])
    return "\n".join(texts), urls


def ask_anthropic(query: str, model: str, key: str) -> tuple[str, list[str]]:
    data = http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model,
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": query}],
            "tools": [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
            ],
        },
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    texts, urls = [], []
    for block in data.get("content", []):
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
            for cit in block.get("citations") or []:
                if cit.get("url"):
                    urls.append(cit["url"])
    return "\n".join(texts), urls


def ask_perplexity(query: str, model: str, key: str) -> tuple[str, list[str]]:
    data = http_json(
        "https://api.perplexity.ai/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": query}]},
        {"Authorization": f"Bearer {key}"},
    )
    text = data["choices"][0]["message"]["content"]
    urls = list(data.get("citations") or [])
    for sr in data.get("search_results") or []:
        if sr.get("url"):
            urls.append(sr["url"])
    return text, urls


def ask_gemini(query: str, model: str, key: str) -> tuple[str, list[str]]:
    data = http_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {
            "contents": [{"parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
        },
        {"x-goog-api-key": key},
    )
    cand = (data.get("candidates") or [{}])[0]
    texts = [
        p.get("text", "")
        for p in cand.get("content", {}).get("parts", [])
        if p.get("text")
    ]
    urls = []
    meta = cand.get("groundingMetadata") or {}
    for chunk in meta.get("groundingChunks") or []:
        uri = (chunk.get("web") or {}).get("uri")
        if uri:
            urls.append(uri)
    return "\n".join(texts), urls


PROVIDERS = {
    "openai": ("OPENAI_API_KEY", "OPENAI_MODEL", "gpt-4o", ask_openai),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude-sonnet-5", ask_anthropic),
    "perplexity": ("PERPLEXITY_API_KEY", "PERPLEXITY_MODEL", "sonar", ask_perplexity),
    "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL", "gemini-2.5-flash", ask_gemini),
}


# ------------------------------------------------------------------- analysis

def domain_of(url: str) -> str:
    m = re.match(r"https?://([^/?#]+)", url.strip())
    if not m:
        return ""
    host = m.group(1).lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def mention_position(text: str) -> tuple[bool, str]:
    """(mentioned, position). Position = rank of the first list item that
    mentions Clarence ('3' = third item), '0' = mentioned in prose, '' = not
    mentioned. Heuristic — good enough for month-over-month movement."""
    if not MENTION_RE.search(text):
        return False, ""
    item_rank = 0
    for line in text.splitlines():
        is_item = bool(LIST_ITEM_RE.match(line))
        if is_item:
            item_rank += 1
        if MENTION_RE.search(line):
            return True, str(item_rank) if is_item else "0"
    return True, "0"


def clarence_citation(urls: list[str]) -> str:
    for url in urls:
        d = domain_of(url)
        if any(d == cd or d.endswith("." + cd) for cd in CLARENCE_DOMAINS):
            return url
    return ""


def slugify(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit]


# ------------------------------------------------------------------------ run

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--providers", default="",
                    help="comma-separated subset (default: all with keys)")
    ap.add_argument("--queries", default=str(QUERIES_FILE))
    ap.add_argument("--limit", type=int, default=0, help="probe only the first N queries")
    ap.add_argument("--sleep", type=float, default=1.0, help="pause between calls")
    ap.add_argument("--check", action="store_true", help="show config status and exit")
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    triples = read_queries(Path(args.queries))
    # Branded (direct-Clarence) questions surface trivially and teach little, so
    # they run only every other session; core and exploration run every time.
    include_branded = (_prior_run_count() % 2 == 0)
    queries = [(c, q) for (t, c, q) in triples if t != "branded" or include_branded]
    if args.limit:
        queries = queries[: args.limit]

    configured = {}
    for name, (key_var, model_var, default_model, fn) in PROVIDERS.items():
        key = os.environ.get(key_var, "")
        model = os.environ.get(model_var, default_model)
        configured[name] = (key, model, fn)

    if args.check:
        core_n = sum(1 for t, _, _ in triples if t == "core")
        expl_n = sum(1 for t, _, _ in triples if t == "exploration")
        brand_n = sum(1 for t, _, _ in triples if t == "branded")
        print(f"queries this run: {len(queries)} in {args.queries}")
        print(f"  core={core_n} exploration={expl_n} branded={brand_n} "
              f"(branded {'INCLUDED' if include_branded else 'skipped'} this run)")
        print(f"clarence domains: {', '.join(CLARENCE_DOMAINS)}")
        for name, (key, model, _) in configured.items():
            status = f"ready (model {model})" if key else f"NO KEY — set {PROVIDERS[name][0]} in env or {HERE / '.env'}"
            print(f"  {name:11s} {status}")
        return 0

    wanted = [p.strip() for p in args.providers.split(",") if p.strip()] or list(PROVIDERS)
    unknown = [p for p in wanted if p not in PROVIDERS]
    if unknown:
        print(f"unknown providers: {', '.join(unknown)}", file=sys.stderr)
        return 2

    active = [(n, *configured[n]) for n in wanted if configured[n][0]]
    skipped = [n for n in wanted if not configured[n][0]]
    if skipped:
        print(f"skipping (no key): {', '.join(skipped)}", file=sys.stderr)
    if not active:
        print(
            f"No provider keys configured. Create {HERE / '.env'} with at least one of "
            "OPENAI_API_KEY / ANTHROPIC_API_KEY / PERPLEXITY_API_KEY / GEMINI_API_KEY, "
            "or export them. Run --check to see status. No rows written.",
            file=sys.stderr,
        )
        return 1

    run_date = date.today().isoformat()
    new_file = not RESULTS_FILE.exists()
    for provider, key, model, fn in active:
        (RAW_DIR / run_date / provider).mkdir(parents=True, exist_ok=True)

    # Concurrent sweep. The engines run in parallel and several queries fire per
    # engine at once, so 700 slow web-search calls finish in ~20-30 min instead
    # of the hours a sequential loop took (which the Mac sleeping then stretched
    # across a whole day). Per-engine caps keep each within its rate limits;
    # Gemini is kept low for its per-minute cap and the shared retry backoff
    # absorbs the occasional 429. CSV writes are serialized under a lock.
    CAPS = {"openai": 5, "anthropic": 5, "perplexity": 5, "gemini": 2}
    lock = threading.Lock()
    stats = {"rows": 0, "mentions": 0, "citations": 0, "errors": 0}
    total = len(active) * len(queries)

    fh = RESULTS_FILE.open("a", newline="")
    writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
    if new_file:
        writer.writeheader()

    def run_one(provider, key, model, fn, cluster, query, i):
        row = {
            "run_date": run_date, "provider": provider, "model": model,
            "cluster": cluster, "query": query, "ok": 1, "mentioned": 0,
            "position": "", "clarence_cited": 0, "clarence_url": "",
            "cited_domains": "", "answer_chars": 0, "raw_path": "", "error": "",
        }
        try:
            text, urls = fn(query, model, key)
            mentioned, pos = mention_position(text)
            curl = clarence_citation(urls)
            domains = []
            for u in urls:
                d = domain_of(u)
                if d and d not in domains:
                    domains.append(d)
            raw_path = RAW_DIR / run_date / provider / f"{i:03d}-{slugify(query)}.txt"
            raw_path.write_text(
                f"query: {query}\nprovider: {provider} ({model})\n"
                f"date: {run_date}\ncitations:\n"
                + "".join(f"  {u}\n" for u in urls)
                + "\n---\n" + text + "\n"
            )
            row.update(
                mentioned=int(mentioned), position=pos,
                clarence_cited=int(bool(curl)), clarence_url=curl,
                cited_domains=";".join(domains), answer_chars=len(text),
                raw_path=str(raw_path.relative_to(HERE)),
            )
        except Exception as e:
            row.update(ok=0, error=str(e)[:300])
        with lock:
            writer.writerow(row)
            fh.flush()
            stats["rows"] += 1
            stats["mentions"] += row["mentioned"]
            stats["citations"] += row["clarence_cited"]
            if not row["ok"]:
                stats["errors"] += 1
            done = stats["rows"]
        flag = " ★" if row["mentioned"] else (" ERR" if not row["ok"] else "")
        print(f"  [{done:3d}/{total}] {provider[:4]:4s} {query[:50]:50s}{flag}")

    # One thread pool PER engine, all engines running at once. This is what makes
    # it truly parallel: a single shared pool would fill with same-engine tasks
    # first (they're grouped) and the others would starve. Each engine gets its
    # own cap; total in-flight = sum(caps).
    def run_provider(provider, key, model, fn):
        with concurrent.futures.ThreadPoolExecutor(max_workers=CAPS.get(provider, 4)) as pool:
            list(pool.map(
                lambda iq: run_one(provider, key, model, fn, iq[1][0], iq[1][1], iq[0]),
                enumerate(queries, 1)))

    print(f"\nConcurrent sweep: {total} calls, {len(active)} engines in parallel "
          f"({', '.join(f'{p}:{CAPS.get(p,4)}' for p,*_ in active)}) …")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as top:
        list(top.map(lambda a: run_provider(*a), active))
    fh.close()

    print(
        f"\n{stats['rows']} rows appended to {RESULTS_FILE.name} "
        f"({stats['mentions']} mentions, {stats['citations']} citations of our "
        f"domain, {stats['errors']} errors)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
