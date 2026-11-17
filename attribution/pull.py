"""Connectors: one per data source, each normalized to the same row shape.

Every connector returns a dict:
  {"status": "ok" | "not_connected" | "error",
   "note": "<human line>",
   "rows": [ {source, campaign, sessions, signups, spend, clicks, impressions}, ... ]}

Design rule: a missing credential is NORMAL, not a failure. A connector with no
token returns status "not_connected" and an empty row list, so the engine runs
end to end today with only the sources you have wired, and each new source
lights up the moment its token lands in the environment. Nothing here needs a
non-stdlib dependency, so it runs on a bare Python. The one exception is the
Google service-account JWT flow (Search Console + GA4), which needs the
`google-auth` package. Until that is approved and installed, those two accept a
short-lived OAuth bearer token via env instead. See the README checklist.
"""

import json
import os
import subprocess
import urllib.request
import urllib.parse

from sources import classify_from_source

SITE = "https://getclarence.app"
UA = {"User-Agent": "clarence-attribution/0.1"}


def _get_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post_json(url, body, headers=None, timeout=30):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={**UA, "Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# --- 1. Conversions: Cloudflare D1 waitlist -------------------------------
# The ground truth. Runs through wrangler, which is already authed on your Mac
# and, in cloud CI, reads CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID from env.
def pull_conversions():
    cmd = [
        "npx", "wrangler", "d1", "execute", "clarence-waitlist",
        "--remote", "--json", "--command",
        "SELECT COALESCE(source,'direct') AS source, "
        "COALESCE(utm_campaign,'(none)') AS campaign, COUNT(*) AS signups "
        "FROM waitlist GROUP BY source, campaign",
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90,
            cwd=os.path.join(os.path.dirname(__file__), "..", "site"),
        )
        blob = (out.stdout or "") + (out.stderr or "")
        if "CLOUDFLARE_API_TOKEN" in blob:
            return {"status": "not_connected",
                    "note": "run in your authed terminal, or set CLOUDFLARE_API_TOKEN in CI",
                    "rows": []}
        if out.returncode != 0:
            return {"status": "error",
                    "note": (out.stderr or out.stdout).strip()[:200] or "wrangler failed",
                    "rows": []}
        payload = json.loads(out.stdout)
        results = payload[0]["results"] if payload else []
        rows = [
            {"source": r["source"], "campaign": r["campaign"],
             "sessions": 0, "signups": int(r["signups"]),
             "spend": 0.0, "clicks": 0, "impressions": 0}
            for r in results
        ]
        return {"status": "ok", "note": f"{sum(r['signups'] for r in rows)} signups",
                "rows": rows}
    except FileNotFoundError:
        return {"status": "not_connected",
                "note": "wrangler not found (run inside site/, or set CLOUDFLARE_API_TOKEN in CI)",
                "rows": []}
    except Exception as e:
        return {"status": "error", "note": str(e)[:200], "rows": []}


# --- 2. Traffic: Cloudflare Web Analytics (cookieless) ---------------------
# GraphQL analytics API. Needs CLOUDFLARE_API_TOKEN (Analytics:Read) +
# CF_ACCOUNT_ID + CF_WEB_ANALYTICS_TAG (the site tag from the Web Analytics
# dashboard). Groups page views by referrer host so we can bucket the source.
def pull_traffic_cloudflare(days=7):
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    tag = os.environ.get("CF_WEB_ANALYTICS_TAG")
    if not (token and tag):
        return {"status": "not_connected",
                "note": "set CLOUDFLARE_API_TOKEN + CF_WEB_ANALYTICS_TAG", "rows": []}
    query = """
    query($tag:String!,$since:String!){viewer{accounts{
      rumPageloadEventsAdaptiveGroups(
        limit:1000, filter:{siteTag:$tag, datetime_geq:$since},
        dimensions:[refererHost]
      ){ count sum{visits} dimensions{refererHost} }
    }}}"""
    # datetime math without Date.now(): CI passes ATTR_SINCE; else last 7 days
    since = os.environ.get("ATTR_SINCE", "")
    try:
        data = _post_json(
            "https://api.cloudflare.com/client/v4/graphql",
            {"query": query, "variables": {"tag": tag, "since": since}},
            headers={"Authorization": f"Bearer {token}"},
        )
        groups = (data.get("data", {}).get("viewer", {})
                  .get("accounts", [{}])[0]
                  .get("rumPageloadEventsAdaptiveGroups", []))
        rows = []
        for g in groups:
            host = g["dimensions"]["refererHost"]
            rows.append({
                "source": classify_from_source("", f"https://{host}"),
                "campaign": "(none)",
                "sessions": int(g.get("sum", {}).get("visits", g.get("count", 0))),
                "signups": 0, "spend": 0.0, "clicks": 0, "impressions": 0,
            })
        return {"status": "ok", "note": f"{len(rows)} referrer groups", "rows": rows}
    except Exception as e:
        return {"status": "error", "note": str(e)[:200], "rows": []}


# --- 3. Traffic + conversions: Google Analytics 4 --------------------------
# GA4 Data API runReport by sessionSource / sessionCampaignName. Needs
# GA4_PROPERTY_ID + a Google bearer token. Proper auth is a service-account JWT
# (needs google-auth, deferred); until then set GOOGLE_OAUTH_TOKEN to a
# short-lived token for a test pull.
def pull_ga4(days=7):
    prop = os.environ.get("GA4_PROPERTY_ID")
    token = os.environ.get("GOOGLE_OAUTH_TOKEN")
    if not prop:
        return {"status": "not_connected", "note": "set GA4_PROPERTY_ID", "rows": []}
    if not token:
        return {"status": "not_connected",
                "note": "GA4 needs a Google bearer token (google-auth service account, or GOOGLE_OAUTH_TOKEN)",
                "rows": []}
    body = {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "sessionSource"}, {"name": "sessionCampaignName"}],
        "metrics": [{"name": "sessions"}, {"name": "keyEvents"}],
    }
    try:
        data = _post_json(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
            body, headers={"Authorization": f"Bearer {token}"},
        )
        rows = []
        for r in data.get("rows", []):
            src = r["dimensionValues"][0]["value"]
            camp = r["dimensionValues"][1]["value"] or "(none)"
            sess = int(r["metricValues"][0]["value"])
            conv = int(float(r["metricValues"][1]["value"]))
            rows.append({
                "source": classify_from_source(src),
                "campaign": camp, "sessions": sess, "signups": conv,
                "spend": 0.0, "clicks": 0, "impressions": 0,
            })
        return {"status": "ok", "note": f"{len(rows)} source/campaign rows", "rows": rows}
    except Exception as e:
        return {"status": "error", "note": str(e)[:200], "rows": []}


# --- 4. SEO: Google Search Console ----------------------------------------
# searchAnalytics query by page + query: clicks, impressions, position. Feeds
# the content lane directly (which pages earn clicks for which searches). Same
# Google-auth story as GA4.
def pull_search_console(days=28):
    token = os.environ.get("GOOGLE_OAUTH_TOKEN")
    if not token:
        return {"status": "not_connected",
                "note": "Search Console needs a Google bearer token (see GA4 note)", "rows": []}
    site = os.environ.get("GSC_SITE", "sc-domain:getclarence.app")
    start = os.environ.get("ATTR_SINCE", "")
    body = {"startDate": start, "endDate": os.environ.get("ATTR_UNTIL", start),
            "dimensions": ["query"], "rowLimit": 500}
    try:
        enc = urllib.parse.quote(site, safe="")
        data = _post_json(
            f"https://searchconsole.googleapis.com/webmasters/v3/sites/{enc}/searchAnalytics/query",
            body, headers={"Authorization": f"Bearer {token}"},
        )
        rows = []
        for r in data.get("rows", []):
            rows.append({
                "source": "search:google", "campaign": r["keys"][0][:60],
                "sessions": int(r.get("clicks", 0)), "signups": 0, "spend": 0.0,
                "clicks": int(r.get("clicks", 0)),
                "impressions": int(r.get("impressions", 0)),
            })
        return {"status": "ok", "note": f"{len(rows)} search queries", "rows": rows}
    except Exception as e:
        return {"status": "error", "note": str(e)[:200], "rows": []}


# --- 5. Ads: Meta Ads Insights --------------------------------------------
# Spend + clicks + conversions per campaign. Lights up the CAC column. Needs
# META_ACCESS_TOKEN + META_AD_ACCOUNT (act_XXXX). This is the one that makes
# every ad dollar readable against signups.
def pull_meta_ads(days=7):
    token = os.environ.get("META_ACCESS_TOKEN")
    acct = os.environ.get("META_AD_ACCOUNT")
    if not (token and acct):
        return {"status": "not_connected",
                "note": "set META_ACCESS_TOKEN + META_AD_ACCOUNT (act_XXXX)", "rows": []}
    params = urllib.parse.urlencode({
        "level": "campaign",
        "fields": "campaign_name,spend,clicks,impressions",
        "date_preset": f"last_{days}d" if days in (7, 14, 30) else "last_7d",
        "access_token": token,
    })
    try:
        data = _get_json(f"https://graph.facebook.com/v20.0/{acct}/insights?{params}")
        rows = []
        for r in data.get("data", []):
            rows.append({
                "source": "paid:meta",
                "campaign": r.get("campaign_name", "(none)")[:60],
                "sessions": int(r.get("clicks", 0)), "signups": 0,
                "spend": float(r.get("spend", 0.0)),
                "clicks": int(r.get("clicks", 0)),
                "impressions": int(r.get("impressions", 0)),
            })
        return {"status": "ok", "note": f"{len(rows)} campaigns", "rows": rows}
    except Exception as e:
        return {"status": "error", "note": str(e)[:200], "rows": []}


CONNECTORS = {
    "conversions (D1)": pull_conversions,
    "traffic (Cloudflare)": pull_traffic_cloudflare,
    "traffic+conv (GA4)": pull_ga4,
    "seo (Search Console)": pull_search_console,
    "ads (Meta)": pull_meta_ads,
}
