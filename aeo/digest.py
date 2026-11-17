#!/usr/bin/env python3
"""Clarence AEO digest — turn results.csv into a plain-English weekly report.

Reads the append-only ledger, compares the most recent run to the one before
it, and writes a short markdown digest: what newly mentions us, what we lost,
what we're still absent from, and who is winning the questions we're not.

    python3 scratch/aeo/digest.py                              # compare last two runs, all queries
    python3 scratch/aeo/digest.py --queries scratch/aeo/watchlist.txt   # restrict to the watchlist
    python3 scratch/aeo/digest.py --notify                     # also fire a macOS notification

Written to scratch/aeo/digests/<run_date>.md and echoed to stdout. Stdlib only.

Comparison is like-for-like: only (query, provider) pairs present in BOTH the
latest run and the previous one count toward "new" / "lost", so a small weekly
pulse never looks like a regression against a big monthly full run.
"""

from __future__ import annotations

import argparse
import csv
import html as _html
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS_FILE = HERE / "results.csv"
DIGEST_DIR = HERE / "digests"

try:
    import sources  # the source drill-down miner (same dir)
except Exception:  # pragma: no cover
    sources = None


def _esc(s) -> str:
    return _html.escape(str(s))


def render_html(*, latest, prev, scope, providers, n_answers, errors, n_queries,
                n_mentions, n_cited, n_q_covered, new_mentions, lost_mentions,
                absent, winners, per_provider, reddit_top=None, reddit_subs=None) -> str:
    """A self-contained, shareable visual report. No external fonts or assets,
    so it opens anywhere and can be sent as-is. Daylight palette to match the
    site."""
    def bar(cur, total, color):
        pct = (cur / total * 100) if total else 0
        return (f'<div class="bar"><div class="fill" style="width:{pct:.1f}%;'
                f'background:{color}"></div></div>')

    prov_rows = ""
    for p in providers:
        ans, ment, cit = per_provider.get(p, (0, 0, 0))
        prov_rows += (
            f'<tr><td class="p">{_esc(p)}</td>'
            f'<td class="n">{ment}<span class="of">/{ans}</span></td>'
            f'<td class="barcell">{bar(ment, ans, "#d9603c")}</td>'
            f'<td class="n cit">{cit}</td></tr>'
        )

    def li_list(pairs, empty):
        if not pairs:
            return f'<p class="empty">{empty}</p>'
        items = "".join(f'<li>{_esc(q)} <span class="prov">{_esc(p)}</span></li>'
                        for q, p in sorted(pairs))
        return f'<ul class="movelist">{items}</ul>'

    win_rows = ""
    for d, c in winners.most_common(15):
        win_rows += (f'<tr><td class="dom">{_esc(d)}</td>'
                     f'<td class="barcell">{bar(c, winners.most_common(1)[0][1], "#6b6258")}</td>'
                     f'<td class="n">{c}</td></tr>')
    if not win_rows:
        win_rows = '<tr><td colspan="3" class="empty">no citations recorded</td></tr>'

    compare = (f'Compared with the previous run ({_esc(prev)}).' if prev
               else 'First run. This is the baseline. That zero is the starting line.')
    absent_items = "".join(f"<li>{_esc(q)}</li>" for q in absent)

    # The Reddit drill-down: the exact threads the engines keep citing, since
    # reddit is the runaway top winner in our space. The human layer.
    reddit_card = ""
    if reddit_top:
        rows_html = ""
        for title, url, nq, example in reddit_top:
            rows_html += (
                f'<li><a href="{_esc(url)}" target="_blank" rel="noopener">{_esc(title)}</a>'
                f'<span class="nq">{nq} question{"s" if nq != 1 else ""}</span>'
                f'<div class="ex">e.g. &ldquo;{_esc(example)}&rdquo;</div></li>'
            )
        subs_line = ""
        if reddit_subs:
            subs_line = ('<p class="foot">Most-cited subreddits: '
                         + ", ".join(f"r/{_esc(s)} ({n})" for s, n in reddit_subs) + "</p>")
        reddit_card = f"""
  <div class="card">
    <p class="eyebrow">Inside Reddit: the threads the engines keep citing</p>
    <ul class="threads">{rows_html}</ul>
    {subs_line}
    <p class="foot">These are the actual human conversations the answer engines lean on for our questions. Read them, and you see what to write against, and where to show up.</p>
  </div>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clarence AEO — {_esc(latest)}</title>
<style>
  :root {{ --paper:#f6f1e8; --card:#fcfaf6; --ink:#2a2520; --soft:#6b6258;
    --hair:#e7dece; --terra:#d9603c; --terra-deep:#993c1d; --green:#1e9e86; --rose:#d43e6e; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--paper); color:var(--ink);
    font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif; }}
  .wrap {{ max-width:760px; margin:0 auto; padding:2.4rem 1.4rem 4rem; }}
  h1 {{ font-family:"Newsreader",Georgia,serif; font-weight:500; font-size:1.9rem;
    letter-spacing:-.01em; margin:0 0 .2rem; }}
  .sub {{ color:var(--soft); font-size:.92rem; margin:0 0 1.6rem; }}
  .card {{ background:var(--card); border:1px solid var(--hair); border-radius:14px;
    padding:1.3rem 1.4rem; margin:0 0 1.2rem; box-shadow:0 1px 3px rgba(42,37,32,.06); }}
  .eyebrow {{ font-size:.72rem; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
    color:var(--soft); margin:0 0 .8rem; }}
  .headline {{ font-family:"Newsreader",Georgia,serif; font-size:1.5rem; font-weight:500;
    line-height:1.35; margin:.2rem 0 .5rem; }}
  .headline b {{ color:var(--terra-deep); font-weight:600; }}
  .compare {{ color:var(--soft); font-size:.92rem; margin:0; }}
  table {{ width:100%; border-collapse:collapse; }}
  td {{ padding:.5rem .6rem .5rem 0; border-bottom:1px solid var(--hair); vertical-align:middle; }}
  tr:last-child td {{ border-bottom:none; }}
  td.p {{ font-weight:600; width:6.5rem; }}
  td.n {{ text-align:right; font-variant-numeric:tabular-nums; font-weight:700; width:3.2rem; white-space:nowrap; }}
  td.n .of {{ color:var(--soft); font-weight:400; }}
  td.cit {{ color:var(--green); }}
  td.dom {{ font-size:.9rem; word-break:break-all; }}
  .barcell {{ width:auto; }}
  .bar {{ background:rgba(231,222,206,.6); border-radius:5px; height:12px; overflow:hidden; min-width:60px; }}
  .fill {{ height:100%; border-radius:5px; }}
  .cols {{ display:grid; grid-template-columns:1fr 1fr; gap:1.2rem; }}
  @media (max-width:560px) {{ .cols {{ grid-template-columns:1fr; }} }}
  .movelist, .absentlist {{ list-style:none; padding:0; margin:0; }}
  .movelist li {{ padding:.3rem 0; border-bottom:1px solid var(--hair); font-size:.92rem; }}
  .movelist li:last-child {{ border-bottom:none; }}
  .prov {{ color:var(--soft); font-size:.8rem; }}
  .empty {{ color:var(--soft); font-style:italic; margin:.3rem 0 0; }}
  details {{ margin-top:.6rem; }}
  summary {{ cursor:pointer; color:var(--terra-deep); font-size:.9rem; }}
  .absentlist {{ columns:2; margin-top:.7rem; }}
  .absentlist li {{ font-size:.85rem; padding:.15rem 0; break-inside:avoid; }}
  .foot {{ color:var(--soft); font-size:.82rem; margin-top:1.4rem; }}
  .green {{ color:var(--green); }} .rose {{ color:var(--rose); }}
  .threads {{ list-style:none; padding:0; margin:0; }}
  .threads li {{ padding:.55rem 0; border-bottom:1px solid var(--hair); }}
  .threads li:last-child {{ border-bottom:none; }}
  .threads a {{ color:var(--terra-deep); text-decoration:none; font-weight:600; }}
  .threads a:hover {{ text-decoration:underline; }}
  .nq {{ color:var(--soft); font-size:.78rem; margin-left:.5rem; white-space:nowrap; }}
  .ex {{ color:var(--soft); font-size:.82rem; font-style:italic; margin-top:.15rem; }}
</style></head><body><div class="wrap">
  <h1>Clarence AEO standings</h1>
  <p class="sub">{_esc(latest)} &nbsp;·&nbsp; {_esc(scope)} &nbsp;·&nbsp; {', '.join(_esc(p) for p in providers)} &nbsp;·&nbsp; {n_answers} answers, {errors} errors</p>

  <div class="card">
    <p class="eyebrow">Where we stand</p>
    <p class="headline"><b>{n_mentions}</b> mentions across {n_answers} answers &nbsp;·&nbsp; <b>{n_q_covered}</b>/{n_queries} questions name us &nbsp;·&nbsp; <b>{n_cited}</b> link citations</p>
    <p class="compare">{compare}</p>
  </div>

  <div class="card">
    <p class="eyebrow">By engine (questions that mention us)</p>
    <table>{prov_rows}</table>
  </div>

  <div class="cols">
    <div class="card"><p class="eyebrow green">Newly mentioning us ({len(new_mentions)})</p>{li_list(new_mentions, "none this run")}</div>
    <div class="card"><p class="eyebrow rose">Lost since last run ({len(lost_mentions)})</p>{li_list(lost_mentions, "none")}</div>
  </div>

  <div class="card">
    <p class="eyebrow">Who is winning the questions we're absent from</p>
    <table>{win_rows}</table>
    <p class="foot">Study these pages: open the raw answer under raw/&lt;date&gt;/&lt;provider&gt;/ and match the shape of what beat us. These are the gaps the content pipeline should fill next.</p>
  </div>
{reddit_card}
  <div class="card">
    <p class="eyebrow">Still absent everywhere ({len(absent)}/{n_queries})</p>
    <details><summary>show the {len(absent)} questions we don't register for yet</summary>
      <ul class="absentlist">{absent_items}</ul>
    </details>
  </div>
</div></body></html>"""


def read_query_set(path: Path) -> set[str]:
    """The queries in a queries/watchlist file (ignores headers and comments)."""
    out: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out


def load_rows() -> list[dict]:
    if not RESULTS_FILE.exists():
        return []
    with RESULTS_FILE.open() as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queries", default="", help="restrict to queries in this file (e.g. the watchlist)")
    ap.add_argument("--notify", action="store_true", help="fire a macOS notification with the headline")
    ap.add_argument("--html", action="store_true", help="also write a shareable visual HTML report")
    args = ap.parse_args()

    rows = load_rows()
    if not rows:
        print(f"no ledger yet at {RESULTS_FILE} — run probe.py first.")
        return 1

    only = read_query_set(Path(args.queries)) if args.queries else None
    if only is not None:
        rows = [r for r in rows if r["query"] in only]
        if not rows:
            print("none of the watchlist queries are in the ledger yet.")
            return 1

    dates = sorted({r["run_date"] for r in rows})
    latest = dates[-1]
    prev = dates[-2] if len(dates) > 1 else None

    def mentioned_pairs(run: str) -> set[tuple[str, str]]:
        return {(r["query"], r["provider"]) for r in rows
                if r["run_date"] == run and r["ok"] == "1" and r["mentioned"] == "1"}

    def cited_pairs(run: str) -> set[tuple[str, str]]:
        return {(r["query"], r["provider"]) for r in rows
                if r["run_date"] == run and r["ok"] == "1" and r["clarence_cited"] == "1"}

    latest_rows = [r for r in rows if r["run_date"] == latest]
    ok_rows = [r for r in latest_rows if r["ok"] == "1"]
    providers = sorted({r["provider"] for r in ok_rows})
    n_queries = len({r["query"] for r in latest_rows})
    n_answers = len(ok_rows)
    errors = len(latest_rows) - n_answers

    ment_latest = mentioned_pairs(latest)
    cited_latest = cited_pairs(latest)

    # Like-for-like: restrict change detection to pairs measured in both runs.
    new_mentions: set[tuple[str, str]] = set()
    lost_mentions: set[tuple[str, str]] = set()
    if prev:
        prev_pairs = {(r["query"], r["provider"]) for r in rows if r["run_date"] == prev and r["ok"] == "1"}
        latest_pairs = {(r["query"], r["provider"]) for r in ok_rows}
        shared = prev_pairs & latest_pairs
        ment_prev = mentioned_pairs(prev)
        new_mentions = {p for p in (ment_latest - ment_prev) if p in shared}
        lost_mentions = {p for p in (ment_prev - ment_latest) if p in shared}

    # Questions where we appear in NO provider this run, plus who wins them.
    by_query_mentioned: dict[str, set[str]] = defaultdict(set)
    for q, p in ment_latest:
        by_query_mentioned[q].add(p)
    all_queries = sorted({r["query"] for r in latest_rows})
    absent = [q for q in all_queries if not by_query_mentioned.get(q)]

    # Noise domains: not real content sources, just engines' own plumbing
    # (Gemini routes citations through vertexaisearch; Google image/user caches).
    NOISE = ("vertexaisearch.cloud.google.com", "googleusercontent.com",
             "google.com/search", "bing.com/search")
    winners: Counter = Counter()
    for r in ok_rows:
        if not by_query_mentioned.get(r["query"]):
            for d in (r["cited_domains"] or "").split(";"):
                d = d.strip()
                if d and "getclarence" not in d and not any(n in d for n in NOISE):
                    winners[d] += 1

    # ---- render ----
    scope = "watchlist" if only is not None else "full query set"
    headline = (
        f"{len(ment_latest)} mentions across {n_answers} answers "
        f"({len(by_query_mentioned)}/{n_queries} questions, {len(cited_latest)} link citations)"
    )

    lines: list[str] = []
    lines.append(f"# AEO digest — {latest} ({scope})")
    lines.append("")
    lines.append(f"Providers: {', '.join(providers)}. {n_answers} answers, {errors} errors.")
    lines.append(f"**{headline}.**")
    if prev:
        lines.append(f"Compared to the previous run ({prev}).")
    else:
        lines.append("First run: this is the baseline, expect near zero. That zero is the starting line.")
    lines.append("")

    lines.append(f"## Newly mentioning us ({len(new_mentions)})")
    if new_mentions:
        for q, p in sorted(new_mentions):
            lines.append(f"- {q}  ({p})")
    else:
        lines.append("- none this run")
    lines.append("")

    lines.append(f"## Lost since last run ({len(lost_mentions)})")
    if lost_mentions:
        for q, p in sorted(lost_mentions):
            lines.append(f"- {q}  ({p})")
    else:
        lines.append("- none")
    lines.append("")

    lines.append(f"## Still absent everywhere ({len(absent)}/{n_queries})")
    for q in absent:
        lines.append(f"- {q}")
    lines.append("")

    lines.append("## Who is winning the questions we're absent from (top 15 domains)")
    if winners:
        for d, c in winners.most_common(15):
            lines.append(f"- {d}  ({c})")
    else:
        lines.append("- (no citations recorded)")
    lines.append("")
    lines.append("_Study these pages: for a question that matters, open the raw answer "
                 "under raw/<date>/<provider>/ and match the shape of what beat us._")
    lines.append("")

    DIGEST_DIR.mkdir(exist_ok=True)
    out_path = DIGEST_DIR / f"{latest}.md"
    out_path.write_text("\n".join(lines))

    print("\n".join(lines))
    print(f"\nwritten to {out_path.relative_to(HERE.parent.parent)}")

    if args.html:
        per_provider = {}
        for p in providers:
            p_ok = [r for r in ok_rows if r["provider"] == p]
            per_provider[p] = (
                len(p_ok),
                sum(1 for r in p_ok if r["mentioned"] == "1"),
                sum(1 for r in p_ok if r["clarence_cited"] == "1"),
            )
        reddit_top, reddit_subs = None, None
        if sources is not None:
            try:
                ranked, subs, _ = sources.top_pages("reddit.com", latest)
                reddit_top = [
                    (sources.reddit_title(u), u, len(qs), sorted(qs)[0])
                    for u, qs in ranked[:8]
                ]
                reddit_subs = sorted(subs.items(), key=lambda kv: -kv[1])[:8]
            except Exception:
                pass
        html_path = DIGEST_DIR / f"{latest}.html"
        html_path.write_text(render_html(
            latest=latest, prev=prev, scope=scope, providers=providers,
            n_answers=n_answers, errors=errors, n_queries=n_queries,
            n_mentions=len(ment_latest), n_cited=len(cited_latest),
            n_q_covered=len(by_query_mentioned), new_mentions=new_mentions,
            lost_mentions=lost_mentions, absent=absent, winners=winners,
            per_provider=per_provider, reddit_top=reddit_top, reddit_subs=reddit_subs,
        ))
        print(f"visual report: {html_path.relative_to(HERE.parent.parent)}")

    if args.notify:
        note = headline
        if prev and (new_mentions or lost_mentions):
            note = f"+{len(new_mentions)} new, -{len(lost_mentions)} lost. {headline}"
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{note}" with title "Clarence AEO — {latest}"'],
            check=False,
        )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
