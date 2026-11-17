# The Reddit collector, on nobody's laptop

Collection used to live on the always-on MacBook Air, on an hourly launchd job.
When the Air slept, collection stopped, and it stopped *silently*: a dead
collector looks exactly like a quiet queue from the board. That is how 45 days
of recruiting were lost between 2026-09-02 and 2026-10-17 without anyone
noticing.

This directory is the same collector, able to run on a GitHub Actions schedule
instead, in `.github/workflows/reddit-collect.yml`.

## What had to be true, and one thing that is not

`scratch/reddit/HANDOFF-cloud-and-phone.md` said to verify one thing before
designing anything: whether Reddit will talk to a cloud host at all. It will
not, logged out. Measured 2026-10-17 from an Actions runner (Azure westus,
egress `172.184.190.243`), against the exact URLs this engine uses:

| what the collector asks for | from a runner | from the founder's Mac |
| --- | --- | --- |
| `www.reddit.com/r/a+b/new/` HTML | **403 Blocked** | 200, 332 KB |
| `old.reddit.com/r/a+b/new/` HTML | **403 Blocked** | 200 |
| `www.reddit.com/r/a/new.json` | **403 Blocked** | n/a |
| `www.reddit.com/r/a/new/.rss` | 200 **once**, then 429 on 22 of the next 23 | n/a |
| `POST /api/v1/access_token` | 401 with a JSON body, so the host serves this IP | 401 |

The HTML path the laptop uses is gated on a residential address. The Atom feeds
answer once and then throttle, because that budget is per IP and a shared runner
address has usually already spent it: 2 of 24 requests succeeded. So there is
**no logged-out path to Reddit from a datacenter**, and the collector here
authenticates or does nothing.

One thing is still unverified, and it is the only real risk left: **whether
authenticated reads work from a runner.** It could not be tested, because no
Reddit app exists yet (`scratch/reddit/reddit_creds.json` is a placeholder with
empty fields). The evidence either side of it is good. The token endpoint serves
the runner a clean 401, and Reddit's quota for an app-only client is per
`client_id` rather than per IP, which is the whole point of the API and makes a
shared runner address stop mattering. But it is evidence, not a result.

So the first thing to do after adding the secrets is the preflight, which proves
it for about a cent:

```bash
gh workflow run reddit-collect.yml -f preflight=true
```

It checks credentials, performs one real listing read, checks the Anthropic key
and the cloud board, and prints exactly what is missing. It spends nothing on
the model. If the listing read comes back empty while the token works, Reddit is
refusing API reads from that address and the collector cannot live here: that is
the one outcome that sends this back to a machine on a home connection.

## Where the store lives, and why

This was the hard question. `scratch/reddit/reddit.db` is a gitignored SQLite
file on whichever Mac collected. A runner starts with an empty disk, so without
somewhere to put the store it cannot dedupe (every run would re-judge the same
top slice and pay again), and it cannot compute the per-subreddit priors that
`store.sub_priors` learns from what the founder actually posted to.

**The store travels as a text SQL dump on a branch of its own, `reddit-store`.**

The alternatives, and why not:

- **An Actions cache.** Evicted after 7 days without a hit, and under the repo's
  10 GB cache ceiling, with no warning. A miss is an empty store, which means
  silently re-judging thousands of posts and paying for it. A cache is a speed
  trick, not storage, and this is data with money attached.
- **An Actions artifact.** Durable only for its retention window, immutable, and
  you have to hunt through the API for the last successful run to find the
  previous one. Gone if that run is deleted.
- **D1, through the Worker.** The right eventual home and the wrong first move.
  D1 today holds a `queue` projection that can be dropped and rebuilt at will,
  plus the append-only `actions` journal. Making it the store means pushing 3,900
  posts up there and reimplementing `unjudged`, `sub_priors` and `build_queue`
  as HTTP endpoints, which is `store.py` rewritten over the network. It also
  makes a local operation depend on the network and makes the Worker a single
  point of failure for collection. Worst of all it would put the irreplaceable
  data behind an API written in one sitting.
- **git**, which wins on the things that matter here. No expiry. Already proven
  in this repo: the AEO workflow has committed its ledger back for months and
  the content pipeline pushed yesterday. No new credential. And every run is a
  diff you can read and revert, which for data encoding the founder's decisions
  beats being elegant.

Details that make it work rather than merely sound right:

- **A text dump, not the `.db` file.** A SQLite file is an opaque blob: git
  stores a whole fresh copy each commit and reports "binary files differ". The
  dump is line oriented, so git deltas it. Measured: the 1.3 MB dump across two
  runs packs into 308 KB, and a bad write shows up in a diff instead of being
  inferred.
- **Pruned.** Full text is kept for anything worth answering, anything the
  founder has touched, and anything not yet judged: 537 of 3,922 rows today.
  The other 3,385 stay forever as skeletons (id, subreddit, verdict, status),
  because that is what stops us paying to judge them twice and what teaches the
  priors. 2.4 MB of dump becomes 1.3 MB.
- **An orphan branch, written with git plumbing.** `hash-object`, `mktree`,
  `commit-tree`, `push`. No checkout, no branch switch, no working tree touched,
  so `main` cannot be affected by it even in principle. This matters in a
  checkout several sessions share.
- **History is bounded.** A run counter rides in the tree as `runs.txt` and the
  branch re-roots every 500 runs, which is about two months at the cadence
  below. The counter is in the tree rather than counted from history because the
  workflow fetches shallow, and `git rev-list --count` against a shallow ref
  always answers 1, so a depth-based check would silently never fire.

### Why the founder's decisions cannot be lost by any of this

The decisions (12 posted, 166 skipped with reasons, 20 saved people, a
187-row journal) are the only irreplaceable data in the system. Posts can be
recollected and verdicts recomputed for cents. There is a near miss on record
from 2026-09-01 in `docs/URGENT.md`, so this is the property the design is built
around.

1. **The runner writes only collector-owned columns.** `pre_score`,
   `llm_score`, `keep`, `why`, `judged_at`, `num_comments`, `last_seen`. Never
   `status`, `acted_at`, `status_reason` or `status_note`. That is the same
   two-writer split `store.merge_posts` was written for, and the reason it never
   carries status across.
2. **Decisions flow the other way, and only in.** `cloud.pull` runs *before*
   collection and replays the cloud journal through `store.apply_action`, which
   compares the time a decision was *made* and refuses to move a status that is
   already current or newer. It asks for the whole journal every time rather
   than tracking a watermark, because that is one less piece of state for an
   ephemeral runner to lose, and losing it is how a decision gets skipped
   forever.
3. **Three independent copies exist, and the runner owns none of them:** the
   Macs' `reddit.db`, the cloud's append-only `actions` table, and
   `scratch/reddit/backups/`. The committed dump is *derived*. If the whole
   `reddit-store` branch were deleted, the worst case is one expensive
   re-judge, not a lost decision.
4. **The dump refuses to write if it would lose anything.** `storefile.dump`
   prunes a *copy*, never the live file, then compares the post count and every
   decision count before and after and raises if any of them moved. A dump that
   quietly dropped a decision is worse than a run that failed.

Verified by round-tripping the real store: 3,922 posts in, 3,922 out, and 12
posted / 166 skipped / 187 actions / 20 saved people identical on both sides.

## What moved, and what stayed

`scratch/` is gitignored in full, so nothing in it can run in Actions. Moved
into the repo:

| file | why |
| --- | --- |
| `store.py` | the schema and every read and write the runner needs |
| `monitor.py` | collection, scoring, the minor guard, judging, queue building |
| `watch.json` | the watchlist and the theme clusters: configuration, not data |

`scratch/reddit/` now holds **relative symlinks** to these three, so there is
one copy and no chance of the laptop and the runner drifting apart. The
originals are backed up at
`scratch/reddit/backups/pre-collector-move-2026-10-17/`.

Code moved; data and secrets did not. `store._data_dir` works out that the
store, `queue.json`, `seen.json` and `reddit_creds.json` still live in
`scratch/reddit`, from this file's own position rather than from a variable
someone has to remember to set. That is deliberate: a forgotten environment
variable would otherwise resolve to an empty database beside the code, and an
empty store is precisely the failure that looks like a quiet day while it
re-judges everything.

The laptop path is unchanged and was checked after the move: the cockpit
imports, `enrich.py` and `replies.py` import, `monitor.py --subs` reads the real
3,922-post store, and all of it still runs on **Python 3.9**, which is what the
Air has. (`py_compile` passing is not the test; the 2026-09-01 note in
`WHERE-IT-RUNS.md` records an annotation that compiled on 3.9 and died when run,
so it was imported and executed on 3.9.6 here.)

## Collection is the same code, with a different way in

There is no second collector. `collect_html` still owns the three passes, the
scoring, the `reads_as_a_minor` guard and the person-word rule. The only thing
that differs under OAuth is the function that hands over a page of posts, so
`multireddit_new`, `group_search` and `sitewide_search` each gained one branch
and nothing else changed.

Two things get *better* when authenticated:

- **Bodies.** A sub's `/new` HTML page carries titles only, so the judge has
  been reading "(not pulled, judge from the title and the subreddit)" for most
  posts. The API returns `selftext`, so it sees what the person actually wrote.
- **Search comes back.** It has been off since Reddit login-walled it on
  2026-08-31 (`SEARCH_OK = False`), and with it went sitewide discovery, which
  is the only way a subreddit nobody thought of gets onto the watchlist. The
  watchlist has only grown by hand since.

## What it costs

Measured, not estimated: **$0.00192 per post judged** (303 posts, $0.5803, 27
batches in the `spend` table).

- A real full day of hourly collection judged 121 posts, about **$0.23**.
- At the 3-hourly cadence that is roughly **$7 a month**.
- The per-run caps (40 conversations, 15 launch) put the worst case near $25 a
  month, and `run.py` enforces a **rolling 30-day ceiling of $20** read from the
  store's own `spend` table. Over the ceiling it keeps collecting and stops
  judging, because the free ranking still stands and a missed post is worse than
  an unjudged one. Both are tunable: `CLARENCE_BUDGET_CONVERSATIONS`,
  `CLARENCE_BUDGET_LAUNCH`, `CLARENCE_SPEND_CEILING_USD`.

The ceiling is there because of the Perplexity key in `docs/URGENT.md`, which
sat dead for two months because nothing checked. This is the same lesson applied
to the opposite failure, a schedule quietly spending more than anyone meant.

**Actions minutes matter more than the API bill.** This repo is private, so
minutes are metered against the 2,000/month free allowance. Eight runs a day at
about 3 billed minutes is roughly 720 minutes a month, which leaves room for the
content pipeline and the AEO sweep. Hourly would need about 2,160 and does not
fit. That, plus cost, is why the cron is `0 */3 * * *`: three hours is still well
inside the window where a thread has few replies, which is what the ranking
rewards.

## The one thing to decide, not configure

Two collectors on one watchlist build two different piles and push two different
queues to the same phone, which `WHERE-IT-RUNS.md` warns about. They would also
each pay to judge the same posts.

The Air has collected nothing since 2026-09-02, so nothing is lost by this
taking over. Once the preflight passes and a couple of real runs look right, the
Air's job should come off:

```bash
launchctl bootout gui/$(id -u)/app.clarence.reddit.hourly
```

Until then the two are not fighting over anything that cannot be rebuilt: the
cloud `queue` table is a projection and the last push wins. The real cost of
leaving both on is double judging, not damage.

Everything on the laptop still works either way. `hourly.sh`, `sync.sh` and
`sync-cloud.py` are untouched, and running `monitor.py` by hand still collects
over HTML from a home connection, which is the only place that works.
