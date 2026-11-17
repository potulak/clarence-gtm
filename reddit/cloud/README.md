# The cloud half

Triage the queue from your phone with both laptops shut.

    https://clarence-reddit.<your-subdomain>.workers.dev/m?k=<your key>

Open it in your Android browser (Samsung Internet is what the founder uses),
then its menu > Add page to > Home screen.

It installs as a real app rather than a bookmark: `public/manifest.webmanifest`
sets `display: standalone`, so it opens with no address bar, and the icons are
`public/icon-192.png` and `icon-512.png`.

Those three files are served WITHOUT the key, deliberately. Android fetches a
manifest and its icons out of band, frequently with no cookie attached, so gating
them makes the page install as a nameless generic shortcut. They give nothing
away: a title and a picture of three bars. Everything else still 401s, which is
checked after every deploy.

The icons are generated, not drawn: `python3` wrote them with `zlib` and
`struct` and no image library, so there is no dependency to install and nothing
to keep in sync with a design file. Regenerate them by editing that snippet in
the session notes rather than hunting for a source file that does not exist.

Print that link, key included, without it passing through a chat window:

    bash scratch/cloud/link.sh

## 2026-10-17: the real board is up here now

Until tonight there were two phone boards and the good one was only reachable
over plain HTTP on the home network. That is what sent an evening into Chrome's
HTTPS upgrade instead of into talking to anyone. There is now ONE board,
`scratch/cockpit/web/m.html`, served by the Mac at home and by this Worker at the
edge, and `sync-cloud.py` stages it into `public/` on every push again.

### What made the jump

Everything the Mac board does, except two things below.

| | |
|---|---|
| the day's shortlist | yes. Computed on the Mac by `shortlist.build` and pushed as `picks`. Not recomputed here: two rankings would drift and the phone would be showing a different day than the laptop |
| the whole post, readable | yes, and this is new on BOTH boards. See the next section |
| counts, the bar, how stale it is | yes, as JSON in `meta` |
| replies to you, and drafting one | yes, including your own earlier comment, which the drafter needs or it writes an opening line at someone mid-thread |
| ask someone to try it, and what came back | yes, through the proxy, tracked against the PERSON so nobody is asked twice |
| saved people | yes, and FIXED. See below |
| communities worth breaking into | yes |
| drafting, revising, the six one-tap fixes | yes, all through the `PROXY` service binding, same as drafting already was. Both voices travel now, so picking "new here" on the phone no longer writes as the founder anyway |
| skip with a reason, undo, mark posted | yes, unchanged |
| a draft you started on the phone | yes, and it now reaches the laptop. It lands in `drafts` here and is merged into the Mac's `state.json` on the next pull |

### What could not, and why

**Reading a thread live** (`/api/reddit/full`, the "Read the full post" button).
Not because this is a Worker. Measured from the Air, on the founder's own home
connection, on 2026-10-17: the per-thread Atom feed answers 403 Blocked every
time however patient, `.json` answers 403, and old.reddit answers its login page
with a 200. `enrich.scrape_thread` is dead on every machine, and it has been
failing quietly on the laptop board too. The endpoint here returns an honest
refusal, and the page now ASKS whether a thread read is possible
(`can_fetch_threads`) and leaves the button out rather than offering a dead end.
The Mac answers yes only when Reddit API credentials exist.

What replaces it is better anyway: `reddit/collector/bodies.py` fills the post
body in during the hourly run, from the one feed that does still answer, so the
body is already on the card when you get there instead of being fetched while
you wait. Measured on the Air: five feeds, 268 seconds, 192 posts filled, the
day's picks going from 0 of 11 readable to 8 of 11. What is still missing is
COMMENTS, and any post older than about a day in a busy subreddit.

**Voice capture** (`/api/reddit/transcribe`). The Mac has Whisper bundled and
transcribes on the device. The proxy in front of the Anthropic key speaks
`/v1/messages` and nothing else, and Claude has no audio input, so there is no
path from here to a transcript. Rather than ship a record button that records
happily and then fails on upload, the page is told `can_transcribe: false` and
leaves the control out. The keyboard's own mic key still works, types straight
onto the phone, and never sends audio anywhere.

That capability question is the whole reason the two views can be one file
again. The page used to decide from `window.isSecureContext`, which answers a
question about the BROWSER and not about the server: over https at the edge that
test passes and there is still nothing on the other end to transcribe with.

### A bug this found: saving a person has never worked

`/api/reddit/prospect` recorded `status: "prospect"` into `actions`. The Mac
replays `actions` through `store.apply_action`, which does
`UPDATE posts SET status = ?`. On the Mac a prospect is a SEPARATE column, set by
`store.set_prospect`, orthogonal to what you did about the thread.

So every save made from the phone either overwrote a real decision or, where the
post had been decided more recently, was dropped by the last-write-wins rule.
Checked on 2026-10-17: eight saves recorded in the cloud since September, and
all seven people they referred to came back `prospect = 0` on the Air. Every one
was lost. The seven saved people that DO exist were saved from the laptop.

Saving is a decision of its own now, in the `decisions` table, replayed through
`set_prospect`. Nothing is at risk in the eight old rows; they simply never
landed.

### The second watermark

`decisions` holds everything the founder decides here that is not a post status:
saving someone, asking them, what came back, clearing a reply. Each replays into
a different place in the Mac's store, so each carries its own payload, and they
are replayed STRICTLY IN ORDER with the mark stopping at the last one that
actually landed. A row this machine cannot record yet parks there rather than
being skipped, because skipping would move the mark past a decision that was
never recorded.

It has its own mark, `.pull-watermark-decisions`, so a parked decision never
holds up a STATUS. Statuses are the irreplaceable half and must not wait.

The one row that parks today is an ask: `store.record_ask` and the `asks` table
came with the replies work and are not on every machine yet. Those sit in the
cloud and replay the moment the store catches up.

## 2026-10-17, later the same night: this directory is in version control

Everything here lived under `scratch/`, which git ignores in full, so the
deployed Worker existed as **one copy on one laptop**. If that laptop had died,
the board the founder triages from would have kept serving at the edge and
nobody could have rebuilt it, changed it, or found out what it did. That is a
single point of failure sitting on the only recruiting loop there is.

So the code moved into the repo at `reddit/cloud/`, exactly the way
`reddit/collector/` moved earlier the same day, and `scratch/cloud/` now holds
**relative symlinks** to these files. One copy, no drift between the two Macs,
and the data stays outside the repo. The originals are kept at
`scratch/cloud/backups/pre-cloud-move-2026-10-17/`.

### What is in git

| file | why it has to be |
| --- | --- |
| `src/worker.js` | the whole cloud API. 49 KB, written in two sittings, and the only copy |
| `schema.sql` | the D1 tables. Without it a rebuilt Worker talks to a database that has none of them |
| `wrangler.toml` | the deploy. `run_worker_first`, `html_handling` and `preview_urls` are each a lesson that cost an evening |
| `public/m.html` | the board itself, and now the ONE copy of it. See below |
| `public/manifest.webmanifest`, `public/icon-192.png`, `public/icon-512.png` | what makes it install as an app rather than a shortcut. Generated once by a `zlib` snippet that no longer exists anywhere |
| `sync-cloud.py` | the only thing that moves data between a Mac and the edge |
| `link.sh` | prints the phone URL with the key, and is the reason the key has never been in a transcript |
| this README | the Worker is rebuildable from the files above; it is usable from this |

`reddit/collector/` took the two other files that were still unversioned after
tonight: `replies.py`, which fetches who answered you, and
`set-inbox-feed.sh`, which stores the feed URL that lets it. Both are collector
code, both run from the hourly job, and both were rewritten tonight.

### What did NOT move, and must not

Data and secrets. Everything in this table stays in `scratch/`, unversioned,
on the machine it belongs to:

| stays | what it is |
| --- | --- |
| `.proxy-token` | mode 600. Also a Worker secret, which is the copy that matters |
| `.pull-watermark`, `.pull-watermark-decisions` | per machine. Copying one skips another machine's decisions |
| `.wrangler/` | local build cache, and it holds a Cloudflare account cache |
| `public/m.html` **in `scratch/cloud`** | the staged deploy copy, not the source. See below |
| `../reddit/reddit.db`, `queue.json`, `seen.json`, `state.json` | real people's posts, and the founder's decisions |
| `../reddit/reddit_creds.json` | mode 600. Reddit app secret plus two inbox feed URLs, each a bearer token for his inbox |
| the cockpit key | `~/Library/Application Support/clarence-aeo/cockpit-key`, outside the repo entirely, where it already was |

### `wrangler.toml` carries a D1 `database_id`, deliberately

It is an identifier, not a credential. Reaching that database needs an
authenticated Cloudflare session for this account: `wrangler`'s own per machine
OAuth login, or an API token. Neither is in the repo, and neither can be derived
from a UUID. The same is true of the `clarence-proxy` service binding, which is
a name resolved inside the account at deploy time. Cloudflare's own documented
practice is to commit this file, and the alternative, a placeholder plus a note
telling you to paste the real id back in, breaks `wrangler deploy` from a fresh
clone, which is the one moment this whole move exists to serve.

What would NOT be safe in here, and is not: `COCKPIT_KEY` and `PROXY_TOKEN`.
Both are Worker secrets, set once with `npx wrangler secret put`, held by
Cloudflare, and never written to a file in this repo. A rebuild from a fresh
clone has to set them again, which is the next section.

### The board is one file now

The phone view used to exist three times: the cockpit's copy, the Worker's
deploy copy, and nothing in git. Tonight the first two were reunified, so now:

    reddit/cloud/public/m.html          the source, in git, the only one to edit
    scratch/cockpit/web/m.html          a symlink to it. What the Mac board serves
    scratch/cloud/public/m.html         a real file, staged by sync-cloud.py, unversioned

The staging copy in `sync_push` stays, and stays doing real work: editing the
source changes the symlink target, and the next sync copies it into the deploy
tree so a `wrangler deploy` cannot ship a stale fork. What it no longer has to
protect against is the two views being edited apart, because there is one file
to edit.

### Rebuilding the Worker from a fresh clone

Everything needed is in this directory, so it is four commands and two secrets:

```bash
cd reddit/cloud
npx wrangler d1 create clarence-reddit        # then put the new id in wrangler.toml
npx wrangler d1 execute clarence-reddit --remote --file=schema.sql
npx wrangler secret put COCKPIT_KEY           # the key from ~/Library/Application Support/clarence-aeo
npx wrangler secret put PROXY_TOKEN           # the draft proxy's token
npx wrangler deploy
```

`public/` is complete in git, `m.html` included, so a clone deploys a working
board without having to find the phone view somewhere else first. Then one
`python3 scratch/cloud/sync-cloud.py --push` from a Mac that has the store fills
the queue back in, because D1 holds a projection and the Mac holds the truth.

The one thing a clone cannot rebuild is the `actions` and `decisions` journals,
the founder's own triage history. Those are replayed down into the store on
every pull, which is why three copies of them exist and why none of them is the
Worker.

## What is up there, and what is deliberately not

Up there: the computed queue, and your decisions.

**Not** up there, on purpose: the collector. Reddit login-walled logged-out reads
on 2026-08-31, and the fix for that is an authenticated session. A logged-in
session running from a datacenter is the single most blockable shape this could
take, and Reddit's stated reason for the wall was scraping. Collection stays on a
Mac, on a residential connection, where it already works.

So "both laptops closed" means **triage always works, collection waits**. That is
the right trade: reading the queue is the thing you want on a train, and a gap
under a day heals itself, because a run reads 24h of `/new`.

## The direction of truth

The same split the two laptops already used, with the cloud standing where the
MacBook Pro stood:

| | owns | flows |
|---|---|---|
| the Mac | posts, scores, verdicts | pushed up, replacing `queue` |
| the cloud | your decisions | pulled down, replayed into `posts` |

Neither writes the other's columns, so there is nothing to merge. `queue` in D1 is
a projection and can be dropped and rebuilt any time. `actions` is the only thing
up there that cannot be regenerated.

**Pull runs before push, always.** Pushing first would republish a post you just
triaged on the phone as though it were still waiting.

## Running it

    python3 scratch/cloud/sync-cloud.py           # pull decisions, then push queue
    python3 scratch/cloud/sync-cloud.py --pull    # decisions only
    python3 scratch/cloud/sync-cloud.py --push    # queue only

`scratch/reddit/hourly.sh` now calls the full sync after every collection, and it
is non-fatal: a failed sync logs and leaves the collector running.

## Deploying a change

    cd scratch/cloud
    npx wrangler deploy

From `scratch/cloud`, where `wrangler.toml`, `src/worker.js` and the icons are
symlinks into `reddit/cloud/`. Verified with `--dry-run` after the move:
wrangler reads a symlinked config, resolves `main` and the assets directory
relative to the symlink rather than to the file it points at, and bundles and
uploads straight through the links. Deploying from `reddit/cloud` works too, and
is what a fresh clone does.

The `cp` that used to be here is gone: `sync-cloud.py` stages `m.html` into
`public/` on every push, so the view the phone gets is whatever the Mac board is
serving. If a deploy ever looks stale, run the sync first.

## Security as it stands

- The Worker checks the key **before** static assets are served
  (`run_worker_first`). Without that the edge would hand out `m.html` with no
  check at all. Verified: every path returns 401 without the key, including
  `/m.html` directly.
- The key is compared with a length-independent compare, and the cookie is parsed
  properly rather than substring-matched, so a cookie whose value merely contains
  `ck=<key>` cannot pass.
- Cookie is `HttpOnly; Secure; SameSite=Lax`. HTTPS is Cloudflare's, so unlike the
  LAN board the key is never in clear on a network.
- `no-store` and `noindex` on every response. `preview_urls = false`, so there is
  one public hostname rather than one per deployed version.
- The key rotated on 2026-08-31, because the previous one had been pasted into
  chat transcripts. The new one has never been printed to a transcript.

**Still worth doing:** put Cloudflare Access in front. Today a leaked key is full
access; with Access the key becomes a second factor behind your email. It is free
and it is a dashboard job, not a code change: Zero Trust, Applications, Self
hosted, `clarence-reddit.<your-subdomain>.workers.dev`, policy = your email only.

## Voice to draft

Tap **Say it** on a card. The box opens and takes focus, so the keyboard is
already up. Tap the **mic key on the keyboard** and talk. Then **Draft my
reply**, then **Copy**, then paste into the Reddit app.

Dictation is the phone keyboard's own, which means it is the same on-device
engine used for texting: no site permission, no audio sent by the page, nothing
for this code to get wrong.

**The in-page Record button was removed on 2026-09-01, and should not come back.**
It used the Web Speech API, which looked like the better option because Android
Chrome supports it and iOS Safari does not. On the founder's actual browser,
Samsung Internet, it failed twice: first it wrote every sentence twice, because
that engine re-delivers results already marked final (fixed by storing finals by
index rather than appending). Then it emitted words in the wrong order, which is
inside the recogniser and not fixable from the page. The keyboard mic had been
sitting there the whole time doing the job properly. If someone is tempted to add
it back, the lesson is that the OS input method is the better surface for
dictation than anything a web page can drive.

What you dictate is the spine of the draft, not a hint. That is the contract
`draft_reply` in `enrich.py` already stated, and the cloud prompt keeps it: your
point, your examples, your opinion, with an explicit instruction not to invent
experiences you did not mention.

The call goes through `clarence-proxy` over a **service binding**, not a URL.
Cloudflare refuses Worker to Worker over the public hostname (error 1042), and
the binding is better anyway: the hop never leaves Cloudflare's network. So the
Anthropic key still exists in exactly one place, behind the credit ledger and
kill switch that were already built.

Spend is capped at the proxy: the phone's token is funded with $5 in the CREDITS
KV, and the proxy returns 402 when it is gone, which the phone shows as plain
English rather than a status code. An account-level billing failure is surfaced
verbatim too, since "draft failed upstream (400)" once sent a debugging session
after entirely the wrong thing.

**The limitation.** The cloud holds the title and a snippet, not the full post
body or the comment thread, because Reddit is login-walled and the Worker cannot
scrape. Desktop drafts see more. The prompt is told about this and instructed to
say so in one line rather than guess when a post is too thin to answer honestly.

`/api/reddit/enrich` returns 501 by design. Enrichment spends Anthropic credits,
and that key belongs in the draft proxy Worker with its existing credit ledger and
kill switch, not copied here where it would double the blast radius. Analysis
stays on the desktop board.

## The open wrinkle: which machine pushes

Right now **this** MacBook Pro pushes. The MacBook Air also still has the hourly
collector loaded, and it has its own copy of the store, so if both run you get two
machines computing two different queues into one cloud table. Last push wins,
which is not corruption (the queue is a projection) but it does mean the phone
shows whichever machine pushed last.

Pick one. The Air is the better pusher, since it is the one meant to be awake.
Moving it there needs two things: `sync-to.sh` to carry this directory over, and a
credential, because `wrangler` authenticates per machine with its own OAuth login.
A scoped D1 API token in a file is the portable answer and avoids a second
interactive login.

Until that is settled, the honest description is: the phone shows what this laptop
last pushed.

