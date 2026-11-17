# Clarence: the go-to-market code

Clarence is a pre-launch Mac app I am building that helps people keep up with
friends they are slow to text back (getclarence.app). This repository is only
its go-to-market side: the systems I built to find out whether anyone would
hear about it, and what each one measured.

I'm not a software engineer and don't write the code myself. I decide what each
system does, Claude Code writes it, and I check the results and run it.

**Status, plainly:** no launch yet, and none of these systems has produced a
waitlist signup (as of late October 2026). Most of the numbers below say a
channel did not work.

About 19,000 lines of Python, JavaScript, shell and SQL (counted 2026-11-16,
not counting data files or docs). Python is standard library only.

## The systems

| Folder | What it does | Runs |
|---|---|---|
| `aeo/` | Asks the OpenAI, Anthropic, Perplexity and Google models a fixed list of questions with web search available. Records whether Clarence is named or cited, which other brands are, and which pages are cited. Questions that name Clarence are reported apart from those that don't | GitHub Actions, monthly since 2026-08-21 |
| `reddit/` | Collects posts from about 80 subreddits, ranks them without a model, has a model judge only the top slice, and queues the few worth answering. Drafts a reply from a thread for me to rewrite and post by hand. Never posts | a Mac on a schedule, with a phone view on Cloudflare |
| `content/` | Drafts answer pages for questions people ask, in one of four voices for one of four personas, and opens each as a pull request for review | GitHub Actions, 3 a week, capped at about 5 |
| `attribution/` | Built to join waitlist signups with traffic, search and ad data by source. Only the Cloudflare sources are connected; Google Analytics, Search Console and Meta Ads are written but not connected, and nothing has been spent on ads | a workflow file is included |
| `site/functions/`, `site/migrations/` | The waitlist and survey endpoints, which tag each signup with its source (30 patterns: AI engines, search, social) | Cloudflare Pages |
| `proxy/` | The metered proxy the app uses for cloud drafting, so the model key stays on the server and never ships in the app | Cloudflare Workers |
| `.github/workflows/` | The scheduled jobs | |
| `docs/gtm/` | The plans and the audit, below | |

Some READMEs inside refer to paths in the main Clarence repository
(`scratch/...`), which is not shared.

## What they measured

**Answer engines (`aeo/results.csv`, `aeo/trend.txt`):**
- 12 runs from 2026-07-05 to 2026-11-01, 5,846 answers.
- Asked about Clarence by name, the models named it in 76 of 77 answers.
- Asked questions that left out its name (182 questions, 5,769 answers), they named it 0 times.
- `aeo/sources.json` lists every page the models cited instead.

**The audit (`docs/gtm/aeo-analysis-2026-10.md`):** I checked the tracker against published research on measuring AI answers and found problems in my own setup:
- The headline coverage number moved with the sampling schedule, not with anything we did.
- One provider's key had been failing for 859 calls without anyone noticing.

The audit's recommendation was not to make answer engines Clarence's main channel.

**Reddit (as of 2026-10-26, from the Reddit store, which is not in this repo):**
- 3,922 posts collected; 537 kept with full text after judging.
- 12 threads answered by hand; 166 skipped with a reason recorded.
- Judging cost cut by a factor of 17 by ranking first and judging only the top slice.
- No signups from Reddit.

**Site and content (as of 2026-10-26):**
- 35 pages live on getclarence.app; 10 of them came from the content pipeline.
- 46,642 page views at the edge from 2026-07-04 to 2026-10-01 (`aeo/cf_daily.csv`). Much of that is bots: single days spike to several thousand.
- No signups, so the attribution engine has had nothing to attribute yet.

## What is left out

- Raw model answers (`aeo/raw/`) and the Reddit store: the collected posts, the commenters and the saved prospects are other people's content and stay out of any repository.
- Machine names, the live phone URL and the database id are replaced with placeholders. No keys were in the code.
- The app itself, my working notes and the commit history.

## Rules the Reddit side follows

From `docs/gtm/reddit-presence-plan.md`:
- One real account.
- Help first: about nine helpful replies for every one that mentions Clarence.
- Say I built it whenever I mention it.
- Never fake a question, use a second account, ask for votes or DM a link.
- Read and post by hand.
