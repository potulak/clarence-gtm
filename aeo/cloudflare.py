#!/usr/bin/env python3
"""Pull getclarence.app edge analytics from Cloudflare and persist it locally.

Cloudflare's free plan only retains a rolling ~30-day analytics window, so this
upserts each day's numbers into cf_daily.csv. Run it on a cadence (the weekly
pulse does) and history accumulates locally past Cloudflare's own retention,
which is what makes a real time-frame analysis possible.

Reads CLOUDFLARE_API_TOKEN / CLOUDFLARE_ZONE_ID from .env next to this script.
Token needs Zone Analytics: Read (+ Account Analytics: Read).

Usage:
    python3 cloudflare.py            # pull, upsert cf_daily.csv, print report
    python3 cloudflare.py --days 60  # widen the pull window (bounded by retention)
"""
import csv, json, os, sys, datetime, urllib.request
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".env")
DAILY = os.path.join(HERE, "cf_daily.csv")
COUNTRIES = os.path.join(HERE, "cf_countries.json")
GQL = "https://api.cloudflare.com/client/v4/graphql"
FIELDS = ["date", "requests", "page_views", "uniques"]


def env(key):
    for line in open(ENV):
        line = line.strip()
        if line.startswith(key + "="):
            return line[len(key) + 1:].strip().strip('"').strip("'")
    return ""


def gql(token, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GQL, body, {
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req))
    if d.get("errors"):
        raise SystemExit(f"Cloudflare GraphQL error: {d['errors']}")
    return d["data"]


def pull(token, zone, days):
    end = datetime.date.today()
    anchor = env("CF_ANCHOR_DATE")  # optional pin for reproducible/backfill runs
    if anchor:
        end = datetime.date.fromisoformat(anchor)
    start = end - datetime.timedelta(days=days)
    q = """query($zone:String!,$start:Date!,$end:Date!){
      viewer{ zones(filter:{zoneTag:$zone}){
        httpRequests1dGroups(limit:365, filter:{date_geq:$start, date_leq:$end}, orderBy:[date_ASC]){
          dimensions{date}
          sum{ requests pageViews countryMap{ clientCountryName requests } }
          uniq{ uniques }
        }}}}"""
    data = gql(token, q, {"zone": zone, "start": str(start), "end": str(end)})
    zones = data["viewer"]["zones"]
    return zones[0]["httpRequests1dGroups"] if zones else []


def upsert_daily(groups):
    rows = {}
    if os.path.exists(DAILY):
        for r in csv.DictReader(open(DAILY)):
            rows[r["date"]] = r
    for g in groups:
        d = g["dimensions"]["date"]
        rows[d] = {"date": d, "requests": g["sum"]["requests"],
                   "page_views": g["sum"]["pageViews"],
                   "uniques": g["uniq"]["uniques"]}
    with open(DAILY, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for d in sorted(rows):
            w.writerow(rows[d])
    return rows


def report(groups, rows):
    # window totals (this pull)
    tr = sum(g["sum"]["requests"] for g in groups)
    tp = sum(g["sum"]["pageViews"] for g in groups)
    tu = sum(g["uniq"]["uniques"] for g in groups)
    countries = Counter()
    for g in groups:
        for cm in g["sum"]["countryMap"]:
            countries[cm["clientCountryName"]] += cm["requests"]
    span = f"{groups[0]['dimensions']['date']} to {groups[-1]['dimensions']['date']}" if groups else "n/a"
    # Snapshot the country tally for the cockpit (refreshed each pull).
    end_date = groups[-1]["dimensions"]["date"] if groups else None
    json.dump({"generated": end_date, "window": span,
               "totals": {"requests": tr, "page_views": tp, "uniques": tu},
               "countries": countries.most_common(20)},
              open(COUNTRIES, "w"), indent=2)
    print(f"\nCLOUDFLARE TRAFFIC  ·  {span}")
    print("=" * 60)
    print(f"  requests   {tr}")
    print(f"  pageviews  {tp}")
    print(f"  uniques    {tu}")
    print(f"\n  local ledger cf_daily.csv now holds {len(rows)} day(s)")
    print("\n  Top countries by requests (this window):")
    tot = sum(countries.values()) or 1
    for name, n in countries.most_common(10):
        flag = "  <- likely datacenter/bot noise" if name in ("NL", "DE", "SG", "HK") and n / tot > 0.15 else ""
        print(f"    {name:5} {n:>6}  ({100*n/tot:4.1f}%){flag}")
    print()


def main():
    token, zone = env("CLOUDFLARE_API_TOKEN"), env("CLOUDFLARE_ZONE_ID")
    if not token:
        raise SystemExit("CLOUDFLARE_API_TOKEN is empty in .env")
    if not zone:
        raise SystemExit("CLOUDFLARE_ZONE_ID is empty in .env")
    days = 30
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    groups = pull(token, zone, days)
    rows = upsert_daily(groups)
    report(groups, rows)


if __name__ == "__main__":
    main()
