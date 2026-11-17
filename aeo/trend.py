#!/usr/bin/env python3
"""Time-frame analysis over the AEO ledger.

Reads the accumulating results.csv (one block of rows per run_date) and prints
how our answer-engine presence has moved over time: mentions, questions we are
named on, coverage %, link citations, and per-provider mentions, run by run.
Also emits a per-question "first seen" timeline so you can tell which questions
we started (or stopped) winning, and when.

Defensive parse: the ledger's error column can contain provider JSON with raw
newlines that break CSV row alignment, so we keep only rows whose run_date is a
real YYYY-MM-DD date and drop the broken continuation lines.

Usage:
    python3 trend.py            # table to stdout
    python3 trend.py --csv      # machine-readable trend, one row per run
"""
import csv, os, re, sys, datetime

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.csv")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TRUE = {"true", "1", "yes", "t"}


def load(path=LEDGER):
    """Yield only well-formed ledger rows (valid date in run_date)."""
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            d = (row.get("run_date") or "").strip()
            if DATE_RE.match(d):
                yield row


def truthy(v):
    return (v or "").strip().lower() in TRUE


def by_run(rows):
    """Collapse rows into per-run metrics, oldest run first."""
    runs = {}
    for r in rows:
        d = r["run_date"].strip()
        run = runs.setdefault(d, {
            "date": d, "providers": set(), "answers": 0, "mentions": 0,
            "citations": 0, "questions": set(), "hit_questions": set(),
            "by_provider": {},
        })
        run["providers"].add(r["provider"])
        run["by_provider"].setdefault(r["provider"], 0)
        q = (r.get("query") or "").strip()
        if q:
            run["questions"].add(q)
        if truthy(r.get("ok")):
            run["answers"] += 1
        if truthy(r.get("mentioned")):
            run["mentions"] += 1
            run["by_provider"][r["provider"]] += 1
            if q:
                run["hit_questions"].add(q)
        if truthy(r.get("clarence_cited")):
            run["citations"] += 1
    return [runs[d] for d in sorted(runs)]


def first_seen(rows):
    """Earliest run_date each question was mentioned on."""
    seen = {}
    for r in rows:
        if truthy(r.get("mentioned")):
            q = (r.get("query") or "").strip()
            d = r["run_date"].strip()
            if q and (q not in seen or d < seen[q]):
                seen[q] = d
    return seen


def pct(n, d):
    return f"{100*n/d:4.1f}%" if d else "  n/a"


def main():
    rows = list(load())
    if not rows:
        print("No well-formed rows in ledger:", LEDGER)
        return
    runs = by_run(rows)

    if "--csv" in sys.argv:
        w = csv.writer(sys.stdout)
        w.writerow(["run_date", "providers", "answers", "mentions",
                    "questions", "questions_mentioned", "coverage_pct",
                    "link_citations"])
        for r in runs:
            w.writerow([r["date"], "|".join(sorted(r["providers"])),
                        r["answers"], r["mentions"], len(r["questions"]),
                        len(r["hit_questions"]),
                        round(100*len(r["hit_questions"])/len(r["questions"]), 1)
                        if r["questions"] else 0, r["citations"]])
        return

    span = f"{runs[0]['date']} → {runs[-1]['date']}"
    print(f"\nAEO TREND  ·  {len(runs)} runs  ·  {span}")
    print("=" * 78)
    hdr = f"{'run':<12}{'providers':<10}{'answers':>8}{'mentions':>9}{'Qs hit':>8}{'coverage':>10}{'cites':>7}"
    print(hdr)
    print("-" * 78)
    prev = None
    for r in runs:
        nq, nhit = len(r["questions"]), len(r["hit_questions"])
        delta = ""
        if prev is not None:
            dm = r["mentions"] - prev
            delta = f"  ({'+' if dm >= 0 else ''}{dm} vs prev)"
        prev = r["mentions"]
        print(f"{r['date']:<12}{len(r['providers']):<10}{r['answers']:>8}"
              f"{r['mentions']:>9}{nhit:>8}{pct(nhit, nq):>10}{r['citations']:>7}{delta}")
    print("-" * 78)
    print("note: runs differ in scope (full query set vs weekly watchlist),")
    print("so read mentions/coverage %, not raw answer counts, across runs.\n")

    # Per-question first-seen timeline (only questions ever mentioned).
    seen = first_seen(rows)
    if seen:
        print("QUESTIONS WE HAVE EVER BEEN NAMED ON (first seen)")
        print("-" * 78)
        for q, d in sorted(seen.items(), key=lambda kv: (kv[1], kv[0])):
            print(f"  {d}   {q}")
        print()
    else:
        print("Never mentioned on any tracked question yet.\n")


if __name__ == "__main__":
    main()
