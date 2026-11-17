"""The join. Pulls every connected source, merges on (source bucket, campaign),
and writes one channel P&L: who arrived, who converted, what it cost, the CAC.

Run:  python3 engine.py            # human table + writes ledger.csv, standings.md
      python3 engine.py --json     # machine output

The whole point is a single row per channel where sessions (denominator),
signups (numerator), and spend (cost) sit side by side, so "which channel
actually works, and at what price" is one glance, not a spreadsheet join.
"""

import csv
import json
import os
import sys

from pull import CONNECTORS

HERE = os.path.dirname(__file__)


def merge(pulls):
    """Fold every connector's rows into a dict keyed by (source, campaign)."""
    ledger = {}
    for result in pulls.values():
        for row in result["rows"]:
            key = (row["source"], row["campaign"])
            acc = ledger.setdefault(key, {
                "source": row["source"], "campaign": row["campaign"],
                "sessions": 0, "signups": 0, "spend": 0.0,
                "clicks": 0, "impressions": 0,
            })
            for m in ("sessions", "signups", "clicks", "impressions"):
                acc[m] += row[m]
            acc["spend"] += row["spend"]
    return ledger


def rollup_by_source(ledger):
    """Collapse campaigns into one row per channel, and compute conv% + CAC."""
    by_src = {}
    for row in ledger.values():
        s = by_src.setdefault(row["source"], {
            "source": row["source"], "sessions": 0, "signups": 0,
            "spend": 0.0, "clicks": 0, "impressions": 0,
        })
        for m in ("sessions", "signups", "clicks", "impressions"):
            s[m] += row[m]
        s["spend"] += row["spend"]
    out = []
    for s in by_src.values():
        s["conv_pct"] = round(100 * s["signups"] / s["sessions"], 1) if s["sessions"] else None
        s["cac"] = round(s["spend"] / s["signups"], 2) if s["signups"] else None
        out.append(s)
    # Signups first (what we care about), then sessions.
    out.sort(key=lambda r: (r["signups"], r["sessions"]), reverse=True)
    return out


def _fmt(v, dash="."):
    return dash if v is None else v


def render_table(channels, statuses):
    lines = []
    lines.append(f"{'CHANNEL':<20}{'SESS':>7}{'SIGNUP':>8}{'CONV%':>7}{'SPEND':>9}{'CAC':>8}")
    lines.append("-" * 59)
    for c in channels:
        conv = f"{c['conv_pct']}%" if c["conv_pct"] is not None else "."
        spend = f"${c['spend']:.0f}" if c["spend"] else "."
        cac = f"${c['cac']:.2f}" if c["cac"] is not None else "."
        lines.append(f"{c['source']:<20}{c['sessions']:>7}{c['signups']:>8}"
                     f"{conv:>7}{spend:>9}{cac:>8}")
    if not channels:
        lines.append("(no rows yet: connect a source or wait for traffic)")
    lines.append("")
    lines.append("SOURCES")
    for name, st in statuses.items():
        mark = {"ok": "[ok]", "not_connected": "[  ]", "error": "[!!]"}[st["status"]]
        lines.append(f"  {mark} {name:<24} {st['note']}")
    return "\n".join(lines)


def write_ledger(ledger, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["source", "campaign", "sessions", "signups",
                    "spend", "clicks", "impressions"])
        for row in sorted(ledger.values(), key=lambda r: (r["source"], r["campaign"])):
            w.writerow([row["source"], row["campaign"], row["sessions"],
                        row["signups"], round(row["spend"], 2),
                        row["clicks"], row["impressions"]])


def main():
    pulls = {name: fn() for name, fn in CONNECTORS.items()}
    statuses = {name: {"status": r["status"], "note": r["note"]}
                for name, r in pulls.items()}
    ledger = merge(pulls)
    channels = rollup_by_source(ledger)

    if "--json" in sys.argv:
        print(json.dumps({"channels": channels, "sources": statuses}, indent=2))
        return

    write_ledger(ledger, os.path.join(HERE, "ledger.csv"))
    table = render_table(channels, statuses)
    with open(os.path.join(HERE, "standings.md"), "w") as f:
        f.write("# Attribution standings\n\n```\n" + table + "\n```\n")
    print(table)


if __name__ == "__main__":
    main()
