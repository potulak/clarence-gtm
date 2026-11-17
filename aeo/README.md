# AEO weekly sweep (cloud)

This is the answer-engine measurement loop for Clarence. Every Monday it asks
the 175 questions in `queries.txt` to four answer engines (OpenAI, Anthropic,
Perplexity, Gemini, all with web search on), records whether Clarence gets
mentioned or cited and which pages beat us, and writes the reports the cockpit
and site read from.

It runs on **GitHub Actions**, not your laptop, so it happens whether or not
your MacBook is open. The workflow lives at
`.github/workflows/aeo-weekly.yml`.

## One-time setup: add the API keys as repo secrets

The job reads its keys from GitHub Actions secrets. Add them once:

1. Open **github.com/potulak/clarence** in your browser.
2. Go to **Settings > Secrets and variables > Actions**.
3. Click **New repository secret** and add each of these (name on the left,
   your key as the value):

   - `OPENAI_API_KEY`
   - `ANTHROPIC_API_KEY`
   - `PERPLEXITY_API_KEY`
   - `GEMINI_API_KEY`
   - `CLOUDFLARE_API_TOKEN`
   - `CLOUDFLARE_ZONE_ID`
   - `CLOUDFLARE_ACCOUNT_ID`

The same values are in your local `.env` (outside the repo). The Cloudflare
three are optional: leave them out and the traffic step just skips.

## Test it without waiting for Monday

Once the secrets are in and this workflow is on the `main` branch:

1. Go to the **Actions** tab on GitHub.
2. Pick **AEO weekly sweep** in the left list.
3. Click **Run workflow**.

It takes about 20 to 30 minutes. When it finishes it commits an updated
`aeo/results.csv` plus refreshed `trend`, `digest`, `sources`, and `cost`
files, and attaches the raw answer transcripts as a downloadable artifact on
that run.

## How the data flows

- `results.csv` is the ledger. Every run appends 700 rows (175 questions times
  4 engines) tagged with the run date, so week-over-week diffs line up.
- `trend.txt` / `trend.csv` are the run-over-run summary.
- `digests/<date>.md` and `.html` are the plain-English report.
- `sources.json` is the durable catalog of every page the engines cited.
- `cost.json` is the per-run spend estimate.
- Raw per-answer transcripts land under `raw/<date>/<provider>/`. They are kept
  out of git (see `.gitignore`) and uploaded as a per-run artifact instead, so
  the repo stays small. Download the artifact from a run when you want to read
  the exact answer that named us.

## Changing the schedule

Edit the one `cron` line in the workflow. GitHub cron is always UTC, so
`0 16 * * 1` is 16:00 UTC Mondays (about 9am Pacific, 12pm Eastern).

## Cost

About 60 dollars a month in API spend at the weekly full-sweep cadence, the same
as before. GitHub Actions itself is free for this.

## Scripts

Pure Python standard library, no dependencies to install:

- `probe.py` runs the sweep (engines in parallel, per-engine rate caps).
- `digest.py` writes the report and can fire a macOS notification locally.
- `trend.py` builds the run-over-run trend.
- `cloudflare.py` pulls edge traffic into `cf_daily.csv`.
- `sources_repo.py` rebuilds the cited-pages catalog.
- `cost.py` estimates per-run spend.
