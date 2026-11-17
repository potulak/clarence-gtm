#!/usr/bin/env python3
"""Estimate what each AEO run cost, from the ledger.

Output tokens are real (derived from the saved answer length). Input tokens and
the web-search surcharges are ASSUMPTIONS, so this is an estimate, not a bill.
The two big drivers are the per-search fees on OpenAI and Anthropic; Gemini
grounding is treated as free (the ~1,500/day free tier easily covers 175/day).

Writes cost.json (per-run + running total) for the cockpit's run log. Rebuild
any time, and the weekly pulse rebuilds it after each run.

    python3 cost.py            # rebuild cost.json + print the per-run table
"""
import csv, json, re
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "results.csv"
OUT = HERE / "cost.json"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Tokens read back per call: the query is short, but web-search results get fed
# into context as input. A midpoint estimate; real value is 1.5k-6k.
INPUT_EST = 3000
# $/1M input, $/1M output, $/call web-search surcharge. Surcharges are estimates.
PRICING = {
    "openai":     {"in": 2.50, "out": 10.0, "search": 0.025},
    "anthropic":  {"in": 3.00, "out": 15.0, "search": 0.020},  # ~2 searches x $10/1k
    "perplexity": {"in": 1.00, "out": 1.0,  "search": 0.005},
    "gemini":     {"in": 0.30, "out": 2.50, "search": 0.0},     # grounding free at this volume
}


def call_cost(provider: str, out_tok: float) -> float:
    p = PRICING.get(provider)
    if not p:
        return 0.0
    return (INPUT_EST * p["in"] / 1e6) + (out_tok * p["out"] / 1e6) + p["search"]


def build() -> dict:
    rows = [r for r in csv.DictReader(LEDGER.open(newline=""))
            if DATE_RE.match((r.get("run_date") or "").strip())]
    per_run: dict = defaultdict(lambda: {"cost": 0.0, "calls": 0,
                                         "by_provider": defaultdict(lambda: {"cost": 0.0, "calls": 0})})
    for r in rows:
        if r.get("ok") != "1":
            continue
        run, prov = r["run_date"], r["provider"]
        try:
            chars = int(r.get("answer_chars") or 0)
        except ValueError:
            chars = 0
        c = call_cost(prov, chars / 4)
        pr = per_run[run]
        pr["cost"] += c
        pr["calls"] += 1
        pr["by_provider"][prov]["cost"] += c
        pr["by_provider"][prov]["calls"] += 1

    out = {"total": 0.0,
           "assumptions": {
               "input_tokens_per_call": INPUT_EST, "pricing": PRICING,
               "note": ("Estimate, not a bill. Output tokens are real (from answer "
                        "length); input tokens and web-search surcharges are "
                        "assumptions. Gemini grounding treated as free.")},
           "runs": []}
    for run in sorted(per_run, reverse=True):
        pr = per_run[run]
        out["runs"].append({
            "run": run, "cost": round(pr["cost"], 2), "calls": pr["calls"],
            "by_provider": {k: {"cost": round(v["cost"], 2), "calls": v["calls"]}
                            for k, v in sorted(pr["by_provider"].items())}})
        out["total"] += pr["cost"]
    out["total"] = round(out["total"], 2)
    OUT.write_text(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    o = build()
    print(f"wrote cost.json: {len(o['runs'])} runs, ${o['total']} total to date")
    for r in o["runs"][:10]:
        by = ", ".join(f"{k} {v['calls']}" for k, v in r["by_provider"].items())
        print(f"  {r['run']}  ${r['cost']:5.2f}  ({r['calls']} calls: {by})")
