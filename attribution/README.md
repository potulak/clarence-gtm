# Attribution engine

Extends the **Measure** lane of the autonomous marketing engine. One place that
joins every marketing data source on a common key and answers the only question
that matters: **which channel actually produces signups, and at what cost.**

Tier 0 (pure measurement). It reads APIs and writes a ledger. It never touches
the outside world, so it is safe to run fully automatic with no approval gate.

## What it joins

| Source | Gives | Auth needed |
| --- | --- | --- |
| Cloudflare D1 waitlist | conversions + first-touch source (ground truth) | wrangler login (have it) / `CLOUDFLARE_API_TOKEN` in CI |
| Cloudflare Web Analytics | sessions by referrer (cookieless denominator) | `CLOUDFLARE_API_TOKEN` + `CF_WEB_ANALYTICS_TAG` |
| Google Analytics 4 | sessions + conversions by source/campaign | `GA4_PROPERTY_ID` + Google auth |
| Google Search Console | SEO clicks/impressions/position by query | `GSC_SITE` + Google auth |
| Meta Ads Insights | spend/clicks per campaign (the CAC column) | `META_ACCESS_TOKEN` + `META_AD_ACCOUNT` |

Output: `ledger.csv` (per source+campaign) and `standings.md` (the channel P&L).

```
CHANNEL                SESS  SIGNUP  CONV%    SPEND     CAC
ai:chatgpt              420      12   2.9%        .        .
search:google           310       7   2.3%        .        .
social:reddit            95       4   4.2%        .        .
paid:meta               800       9   1.1%     $140    $15.56
direct                  210       3   1.4%        .        .
```

## The one rule that makes attribution work: tag every link

The join key is `utm_campaign`, and the source bucket comes from `utm_source` +
referrer. Attribution is only ever as good as the tagging discipline upstream.
The convention, so every lane tags the same way and the join lines up:

- **Ads:** `utm_source=meta` `utm_medium=paid` `utm_campaign=<campaign-slug>`
- **Reddit:** `utm_source=reddit` `utm_medium=social` `utm_campaign=<thread-or-post-slug>`
- **Content links you share:** `utm_source=<where>` `utm_medium=owned` `utm_campaign=<page-slug>`
- **Email:** `utm_source=newsletter` `utm_medium=email` `utm_campaign=<send-slug>`

Organic AI/search traffic carries no UTM (nobody tags it), so it is classified
from the referrer host, identical to the site's live classifier in
`site/functions/api/waitlist.js`. Keep `sources.py` in sync with that file.

## Run it

```bash
cd attribution
python3 engine.py          # table + writes ledger.csv, standings.md
python3 engine.py --json   # machine output
```

Runs today with zero credentials: every unconnected source shows `[  ]` and the
engine still produces a (currently near-empty) table. Each source lights up the
moment its token is set. No dependency install required for D1 / Cloudflare /
Meta (stdlib only).

## Your checklist to light up each source (Tier 3, founder only)

These are credentials only. No downloads, no card access, no account the engine
creates. Add each as a GitHub Actions secret for the cloud cron, and export it
locally for a test pull.

1. **Cloudflare (traffic + conversions in CI).** Create an API token scoped
   `Account Analytics: Read` + `D1: Read`. Set `CLOUDFLARE_API_TOKEN`,
   `CLOUDFLARE_ACCOUNT_ID`. Turn on Web Analytics for getclarence.app and copy
   the site tag into `CF_WEB_ANALYTICS_TAG`. (Cloudflare Web Analytics is
   cookieless: no consent banner, on brand.)
2. **Google Search Console.** Verify getclarence.app (DNS TXT record). This is
   free, cookieless, and the SEO measurement surface for the content lane. Set
   `GSC_SITE=sc-domain:getclarence.app`.
3. **Google Analytics 4.** Create a GA4 property, set `GA4_PROPERTY_ID`. GA4
   uses Google cookies, so it needs a **consent banner on the site** (a new
   user-facing surface) before the gtag snippet goes live. That is a separate,
   approve-first change, not part of this Tier 0 engine.
4. **Google auth for GSC + GA4.** Proper flow is a service-account JSON read via
   the `google-auth` package. Installing that dependency needs your explicit
   go-ahead (repo rule on downloads), so it is deferred: for a first test pull,
   set `GOOGLE_OAUTH_TOKEN` to a short-lived token from the OAuth playground.
5. **Meta Ads.** Once you set an ad budget cap and a campaign runs, create a
   long-lived access token with `ads_read` and set `META_ACCESS_TOKEN` +
   `META_AD_ACCOUNT=act_XXXX`. This fills the SPEND and CAC columns so every ad
   dollar maps to signups.

## Honest note on sequencing

With ~0 signups and little traffic today, this attributes near-zero right now.
Its value is being the instrument you switch on **before** you spend, so the
spend is readable, and closing the SEO measurement gap for content you already
ship. Build the read side now (done); light up ads later.
