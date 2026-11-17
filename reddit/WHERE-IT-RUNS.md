# Where it runs

**The always-on MacBook Air is the collector**, hourly, and it is the only
machine that can be. Reddit answers 403 to logged-out reads from a datacenter, so
a residential address is the way in, and the credentials that would let a runner
authenticate instead are not available and are not coming: see the rule in
CLAUDE.md. For one afternoon on 2026-10-17 this file said Actions was the
collector. It never collected anything; see the correction further down.

    the Air     collects, judges, pushes to the cloud. Hourly, plugged in, lid may be shut
    Actions     the WATCHDOG only, which needs no Reddit access. reddit-collect is dormant forever
    this Mac    a workbench. Read the board, run things by hand, deploy the Worker
    the cloud   serves the phone. Holds a projection of the queue and YOUR decisions
    your phone  https://clarence-reddit.<your-subdomain>.workers.dev/m   (bash scratch/cloud/link.sh)

`reddit-collect.yml` would need two repository secrets that will never exist, so
it has no `schedule:` and can only ever be run by hand, where it stops in about
eleven seconds. It is kept rather than deleted because the collector code it
calls is shared with the Air, not because it is waiting for anything.

What watches for silence from here instead is `reddit-watchdog.yml`, which needs
no Reddit access at all, only COCKPIT_KEY. That is the part of the 45 days worth
fixing: collection breaking was survivable, nobody being told was not.

The phone works with **every laptop shut**. Collection does not, and cannot.

## The hourly run fills in the post bodies now

`reddit/collector/bodies.py`, added 2026-10-17, between `replies.py` and the
cloud sync. The listing the collector reads carries a title and no body at all,
so the board was a pile of headlines and the judge was reading "(not pulled,
judge from the title and the subreddit)" for most posts.

Reading the thread itself is no longer possible. Measured here, on the home
connection, that evening: the per-thread Atom feed answers 403 Blocked every
time however patient, `.json` answers 403, and old.reddit answers its login
page. A residential address does not help, so `enrich.scrape_thread` and the
board's "Read the full post" button are dead on every machine.

What still answers is a SUBREDDIT's own feed,
`www.reddit.com/r/<sub>/new/.rss?limit=100`: 100 entries, a full body on nearly
every one, at one read per 60 seconds per address (measured: answered at 1s, 429
at 21s and 41s, answered at 62s). So the unit of spend is a subreddit, not a
thread, which is what makes it affordable. It reads the subreddits the day's
shortlist actually draws from, most wanted first, and stops itself at fifteen
minutes so it can never run into the next hourly pass.

Measured on this machine: five feeds, 268 seconds, 192 posts filled, the day's
picks going from 0 of 11 readable to 8 of 11, and 110 unjudged posts gaining a
real body for the next judge.

It writes `posts.post_text` and nothing else, so it is safe beside everything
else and safe to run twice.

## store.py, monitor.py and watch.json are symlinks now

They live in the repo at `reddit/collector/`, because `scratch/` is gitignored
and Actions cannot run what is not committed. What is here are relative symlinks
to them, so there is one copy and the laptop and the runner cannot drift apart.
The originals are at `backups/pre-collector-move-2026-10-17/`.

Data and secrets did NOT move. `store._data_dir` works out that `reddit.db`,
`queue.json`, `seen.json` and `reddit_creds.json` still live here, from the
module's own position, so nothing needed reconfiguring and a forgotten variable
cannot silently point the collector at an empty database.

`sync-to.sh` sends `reddit/collector/` first for this reason: `rsync -a` copies
a symlink as a symlink, so the real files have to be over there or the links
land pointing at nothing.

## The board and the rest of the engine are symlinks now too

Third and last pass, 2026-10-17. `reddit/cockpit/` took the local board on 4360
(`server.py`, `outreach.py`, `web/index.html`), and `reddit/collector/` took
`enrich.py`, `shortlist.py`, `hourly.sh` and `sync-to.sh`. These three documents
moved up to `reddit/`, because they describe the whole loop rather than one half
of it. Everything keeps its old path in `scratch/` as a relative symlink, so
nothing that calls those paths had to change. Originals in
`backups/pre-cockpit-move-2026-10-17/`.

Data and secrets did not move, again, and this pass needed more care about that
than the others did:

- `server.py` worked out `scratch/` as "the directory above me". From
  `reddit/cockpit/` that would have been `reddit/`, so `state.json` and the
  store would have resolved to files that do not exist. It now derives the
  checkout from its own position and puts the data at `<checkout>/scratch`,
  which is the same answer from either home.
- `enrich.py` read `reddit_creds.json` and appended the ICP corpus **next to its
  own code**. Moved as-is, it would have looked for the credentials in the repo
  and written real people's words into a tracked directory. It takes its data
  dir from `store.DATA` now, so there is one answer to that question.
- `outreach.py` had the same shape for the First-10 ledger, which is a file of
  real handles. Same fix.

`hourly.sh` no longer guards its steps with `-f`. `-f` is false for a symlink
pointing at nothing, so a broken link would have made a step vanish from the run
in silence, and a collector that skips a step looks exactly like a quiet hour.
It checks with `-e` and prints a line when something is missing.

## And the last nine scripts, 2026-10-17

Fourth pass, the clean-up. Ten files had been left behind by the third because
nobody had read them. Nine were live and moved; one was dead and stayed.

    scratch/reddit/archived.py        -> reddit/collector/   (the Refresh button)
    scratch/reddit/awake.sh           -> reddit/collector/
    scratch/reddit/{sync,pull-data,push-actions}.sh  -> reddit/collector/
    scratch/reddit/{setup-machine,push-repo}.sh      -> reddit/collector/
    scratch/reddit/{cockpit,offline}.sh              -> reddit/cockpit/

`monitor_public.py` stayed on the laptop. It is the ancestor of `monitor.py`,
which does everything it does and more, nothing calls it, and it writes
`queue.json` into the working directory, so running it from `scratch/reddit`
would flatten the live queue. History, not code.

Nothing needed a new directory, so `sync-to.sh` needed no change: it already
sends `reddit/collector/` and `reddit/cockpit/` whole, and in that order.

Three things beyond copying, all the same shape as the passes before it:

- `archived.py` kept `queue.json` **next to its own code**, which was the right
  answer only while the code sat in the data directory. It takes `store.DATA`
  now, the same answer `monitor.py` writes the queue with. The cockpit runs it
  by path, so a stale copy would have tagged a queue nobody reads.
- `sync.sh`, `pull-data.sh` and `push-actions.sh` imported `store` through
  `scratch/reddit`, which is a symlink into the repo now. They import it from
  `reddit/collector` instead, on both sides of the ssh, so they work on a
  checkout where the links have not been laid yet.
- `setup-machine.sh` pointed its two launchd jobs at the `scratch/` paths. They
  point at the repo copies now. launchd following a link that resolves to
  nothing fails on the hour, into a log nobody reads, and a collector that is
  not running looks exactly like a quiet week.

`scripts/no-emdash.sh` covers `reddit/` whole from this pass, rather than three
named paths. `assist.py` and `monitor.py` each carried a literal em dash inside
the scrubber that strips em dashes; both are `\u2014` escapes now, the way
`enrich.py` already was. Worth knowing: `monitor.py` had the same scrubber
written twice, once escaped and once not, which is the argument for the gate
covering the directory rather than a list of files.

## Do not run the collector here

`app.clarence.reddit.hourly` is deliberately NOT loaded on this Mac. Two
collectors build two different piles from one watchlist, and now they would also
push two different queues to the same phone. Check with:

    launchctl list | grep clarence.reddit      # expect nothing on this Mac

The same now applies to the Air. Once the Actions runs look right, take its job
off so the two are not each paying to judge the same posts:

    ssh you@your-mac.local \
      'launchctl bootout gui/$(id -u)/app.clarence.reddit.hourly'

Leaving both on damages nothing, because the cloud `queue` table is a projection
and the last push wins. It just costs twice.

Running `monitor.py` here by hand is fine and does not change that. It writes to
this machine's store, which then diverges from the Air until you sync.

## Sending code to the Air

    bash reddit/collector/sync-to.sh you@your-mac.local

(`scratch/reddit/sync-to.sh` still works, and is the same file.)

Never with `--with-data`. That overwrites the Air's store, including every posted
and skipped decision, which is the one thing here that cannot be rebuilt. On
2026-09-01 the Air held 5 decisions this Mac did not, and an overwrite would have
destroyed them; they were rescued by replaying them through `store.apply_action`
first. If you ever must hand the store over again, merge in that direction FIRST
and verify the sender is a strict superset.

Two traps that cost time on 2026-09-01, both worth knowing:

- **`rsync` silently did not overwrite `reddit.db`** on the Air, while reporting
  a successful transfer and writing every other file. `scp` to `/tmp` and then
  `mv` into place worked, and is verifiable. Always check `md5` on both ends
  afterwards rather than trusting the transfer's own output.
- **The Air runs macOS system Python 3.9**, this Mac runs 3.14. A `dict | None`
  annotation in a function signature is evaluated at import time before 3.10, so
  anything sent over there needs `from __future__ import annotations`. It will
  pass `py_compile` on 3.9 and still die when it runs, so test by RUNNING it.
  Caught again on 2026-10-17, and it had been live for a while: `outreach.py`
  returns `dict | None` from one function and had no `__future__` line, so it
  raised on import on the Air. The cockpit imports it lazily inside a `try`, so
  nothing failed loudly: the First-10 tab was simply absent on the machine that
  is always up. A grep for `|` in PARAMETER annotations misses this one, because
  it is a return annotation. Check both positions.

## Reading the data from here

    bash scratch/reddit/sync.sh          # trade with the Air, both directions
    bash scratch/reddit/pull-data.sh     # one-way snapshot for querying

Note `sync.sh` carries posts and verdicts one way and your decisions the other.
It does NOT carry decisions made ON the Air back to here, because `merge_posts`
deliberately never overwrites status. Decisions now travel through the cloud
instead: the Air pulls them down every hour with the rest of the sync.

## The archived tagger was reading the login page, 2026-10-18

`archived.py` is the Refresh button's second step, and it had gone quietly wrong
in exactly the way the measurement above predicts. It fetched the old.reddit
version of each queued thread and returned True if the page carried an archived
marker, False otherwise, with None only when the fetch itself raised. A login
page does not raise and carries no marker, so it returned False, and the board
reads False as "you can reply to this".

Measured here that morning, through the function's own code path: four real
queued threads and one post id invented on the spot all answered the same way.
HTTP 200, about 333KB, redirected to `old.reddit.com/login/?reason=lor2`, titled
"Welcome to Reddit", and `is_archived` said commentable for all five. It said you
could reply to a thread that does not exist.

Three things changed, and one deliberately did not.

- **The fetch recognises the page it got.** It returns None, not False, for a
  login wall (the final URL or the "Welcome to Reddit" title, which is the pair
  `enrich.scrape_thread` already refuses on) and for anything with no sign of
  being a comments page. The positive markers are the vote arrows, `class="md"`,
  `commentarea` and `id-t3_`: not guesses, because the archived class is read out
  of the arrow markup and `enrich` parses the post out of `class="md"`, so a
  thread page carrying none of them could never have been tagged in the first
  place. The login page carries none of the four, across all 333KB.
- **A pass stops at the wall and writes nothing.** The wall is a property of the
  machine, not of the thread, so the first one ends the pass: one request instead
  of 21, and the live queue keeps no answers rather than gaining 21 wrong ones.
  Unknown threads are left with no field at all, deliberately, so the next pass
  retries them. Writing a null would have stuck them unknown until someone
  remembered `--refresh`.
- **Refresh no longer calls it.** It cannot learn anything today, and the field
  only ever reached `compute_content_plan` and the queue.json fallback anyway:
  the board's reply queue and Voice lab come from `_build_reddit_queue`, off the
  store, and `posts` has no archived column. So the automatic pass was 21
  requests feeding a panel nobody splits on. It is a hand tool now, with a
  `--selftest` that checks the rule on eight pages without a network, because the
  rule can no longer be checked against a live thread: a version that called
  every real thread unknown would look exactly as healthy as a correct one.

What did not change: unknown threads stay in the **reply queue**, not the Voice
lab. That was the old default and it is still the right one. These posts come off
a sub's /new feed hours old, Reddit stopped auto-archiving by default years ago,
and being wrong costs one click to discover. Sending 21 unknowns to the Voice lab
would empty the reply queue, which is the point of the board.

If the split is worth having again, it should not come from here. The collector's
OAuth listing already carries `archived` and `locked` on every post, for free, in
`monitor._from_api`. A column on `posts` filled from there would reach the board
and the phone, which this file's output never did.

## The split, from the listing that was already carrying it, 2026-10-18

The paragraph above says the route to a real archived/commentable split is the
OAuth listing, not a thread fetch. This is that route, built the same day.

`monitor._from_api` has been handed `archived` and `locked` on every post in
every listing it has ever read, and kept neither. So the fact was arriving,
hourly, free, and being thrown away, while the board's two piles were fed by 21
HTTP requests per Refresh that have not been able to answer since the 17th.

What moved, end to end:

- **Two columns on `posts`**, `archived` and `locked`, added by the same
  `if "x" not in cols` guard as `author`, `created_utc` and `post_text`, so it
  is safe to run twice. **With no DEFAULT**, which is the point of them: NULL
  means nobody has told us. A `DEFAULT 0` would have turned all 5,327 posts
  already in the store, and every post a laptop collects from now on, into a
  claim that you can reply to it, which is the bug of this morning arriving from
  the other direction.
- **`_from_api` keeps both**, through a small `flag()` that keeps None as None
  and turns SQLite's 1 into a real boolean. That second job is load-bearing: the
  board asks `t.archived === true`, and in JavaScript `1 === true` is false, so a
  thread served straight out of the store as a 1 would sit in the reply queue
  wearing a lock.
- **Neither collector can overwrite the other with a shrug.** `upsert` and
  `merge_posts` both write these as `COALESCE(?, column)`: a source that knows
  wins, including True becoming False when a mod unlocks a thread, and a source
  that does not know passes NULL and changes nothing. The HTML path on a laptop
  has no answer to give, and leaves the field alone rather than filling it with
  a no.
- **`build_queue` serves both on every thread**, so `_build_reddit_queue` does
  too, which is the board's own source. The board keeps its rule and extends it:
  `locked` now keeps a thread out of the reply queue alongside `archived`.
- **The phone gets them on `picks`**, which is the table its deck is drawn from,
  plus `queue` for the full-queue projection. `ensureSchema` adds both to a live
  D1, so no `wrangler d1 execute` is owed. `sync-cloud.py` needed no change at
  all: `shortlist.build` and `store.queue` both select the whole row, so the
  columns were already in the payload the moment the store had them, and the
  Worker was dropping them for want of two names in `PICK_COLS` and
  `QUEUE_COLS`. The phone has one pile rather than two, so a closed thread is
  not hidden from the deck, it is marked on the card, before five minutes go
  into dictating an answer that cannot be posted.

### Why `locked` joins `archived`

They are not the same fact. `archived` is Reddit's own age lock: comments AND
votes closed on old posts, and only in the subs whose mods leave archiving
switched on, which is a minority since Reddit stopped doing it by default.
`locked` is a moderator (or the author) closing one thread, which stops new
comments, leaves the existing ones, and can be lifted again.

Different reasons, one answer to the only question the reply queue asks: this
thread will not take your comment. A locked thread in the reply queue costs
exactly what an archived one costs, a draft written and a 403, so both belong in
the Voice lab, where they are worth reading. Because a lock is reversible and
the column is refreshed on every listing read, a thread that gets unlocked comes
back on its own: the next read writes False and the COALESCE above lets it.

### What is proven, and what is not

Proven, against a copy of each machine's own store, on both Macs, under the
Air's Python 3.9.6: the migration runs twice without complaint and adds no
default; a listing that says archived, locked, open, or nothing produces True,
True, False and None; an HTML pass over the same posts changes nothing; a later
listing can flip a True to False; a merge fills what is missing and never blanks
what is known; `build_queue` serves real JSON booleans and nulls; and the
board's own filter text, run on all six shapes a thread can now arrive in,
splits them the right way and keeps unknown in the reply queue.

Not proven, and it cannot be from here: that the live listing's field names are
these. Neither Mac holds the Reddit app credentials, and every logged-out JSON
path answers 403, from this home connection as well as from a runner, measured
again today. So the test feeds the exact listing shape by hand. The first real
fill is visible immediately when it happens: `SELECT COUNT(*) FROM posts WHERE
archived IS NOT NULL` goes from 0 to the size of that run.

CORRECTION, written the same day a few hours later. The paragraph above said in
its first version that the credentials are Actions secrets now. They are not.
Checked: `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` are not set on the
repository, and every scheduled run of `reddit-collect` has failed in about
eleven seconds with "STOPPING: no Reddit app credentials", which is `run.py`
refusing to collect rather than collecting nothing. Three runs on 2026-10-17, at
17:50, 19:58 and 22:49, all of them. There is no `reddit-store` branch, so the
runner has never written a store at all.

SECOND CORRECTION, 2026-10-18. The paragraph above read as though those two
secrets were a pending errand. They are not available at all, which the founder
has said more than once, and it is now a rule in CLAUDE.md rather than a note.

So this is settled, not open: no machine anywhere reads Reddit over OAuth, both
Macs collect over HTML, HTML cannot answer whether a thread is archived or
locked, and the only component that was ever going to has never started and
never will. `archived` and `locked` stay NULL, the Voice lab stays empty, and
`import-store.py` has nothing to bring down. The columns cost nothing to carry
and are honest where they sit, which is why they stay.

Reddit changing its own rules on unauthenticated reads is the only thing that
reopens any of this.

## The way down from the runner, 2026-10-18

`reddit/collector/import-store.py` is the missing half of the sentence above:
once the runner does collect, this brings what it learned into a Mac's store.

The runner's store travels as a SQL dump on the `reddit-store` branch and
nothing read it. `sync.sh` trades between the two Macs, and no script read that
branch, so everything the runner learns was staying up there. This fetches the
branch into a remote-tracking ref (no checkout, no branch switch, because this
runs in the tree the live board reads), loads the dump into a store of its own in
a temporary directory, and hands its rows to the same `store.merge_posts` that
the two Macs already use on each other. One merge rule, one set of answers about
who owns which column.

Two things make it safe to run on the only irreplaceable data here. It writes
nothing without `--apply`: the default does the whole merge against a copy and
reports what would land. And it proves rather than trusts the thing that matters,
reading the posted, skipped, journal and saved-people counts before and after and
refusing to call the run a success if any of them moved.

It stays a hand tool, not another step in `hourly.sh`, for the same reason
`archived.py` became one: there is nothing for it to read yet, and a step that
can only fail is worse than no step. The Air cannot run the fetch half at all,
because its git has no GitHub credentials ("could not read Username for
https://github.com"), so over there it takes `--from` and a dump carried across
by hand.

Rehearsed against a copy of the laptop's real store with a dump manufactured by
`storefile.dump` from a store marked up the way an OAuth run would mark it: 2 new
posts, 13 threads learning whether they are archived, 6 of them closed, and the
board built off the result split 78 to the reply queue and 6 to the Voice lab
with the other 72 unknown and still answerable. The dry run left the copy's
checksum unchanged, and all four decision counts matched on both passes.

## The battery guard, 2026-10-18

`awake.sh on` is the only way to collect with the lid shut, because SleepDisabled
is the one switch a closed lid respects. It is also global: macOS offers no "never
sleep on the charger but do on battery". So an unplugged Mac with it on does not
nap, it runs flat and stops dead, and a collector that is off looks exactly like a
quiet week.

That was the live state on the morning of 2026-10-18. The Air was unplugged with
SleepDisabled on, 16 hours of battery left, and nothing watching it. Earlier the
same morning, before the switch went on, it had been sleeping instead: 3 runs
that day (00:32, 01:36, 09:16) and 8 the day before, against an hourly job. Either
failure is silent, and they are opposite failures of the same setting.

`reddit/collector/battery-guard.sh` is the seatbelt. Below the floor on battery it
re-enables sleep so the machine naps instead of dying; back on the charger it puts
the switch back.

    bash reddit/collector/battery-guard.sh status      # what it sees, what it would do
    bash reddit/collector/battery-guard.sh selftest    # the 12 rules, no machine
    sudo bash reddit/collector/battery-guard.sh install # watch every 5 minutes

The rule that makes it safe to leave running: **it only ever undoes its own
change.** Lowering the switch writes a hold file, and only a hold file lets it
raise the switch again, so a deliberate decision to let the machine sleep is never
reversed behind you and the guard can never pick the policy. Twelve cases are
pinned in `selftest`, including the three that say "do nothing" where a careless
version would act.

A LaunchDaemon rather than an agent, and not a step inside `hourly.sh`, because
only root may set that switch and both of those run as the user. Floor is 20%,
overridable with `CLARENCE_BATTERY_FLOOR`.

Two things worth knowing before trusting it:

- **The gap it cannot close.** Once it has lowered the switch and the machine is
  asleep with the lid shut, no daemon of ours is running. Plugging in generates a
  wake, launchd then runs the overdue check and the switch goes back up, which is
  the path that matters. If that wake is too brief, nothing restores it until the
  next wake. Open the lid once if the board looks stale after a recharge.
- **The watchdog will not catch a short outage.** It allows 12 hours of silence
  before it complains, and it reported "OK: collected 6.8h ago" straight through
  the 7.6 hour gap described above. For an hourly collector that threshold is
  loose, and tightening it is a separate change.

Verified on both Macs: 12 selftest cases pass on this Mac's bash and on the Air's
bash 3.2.57, and the install and uninstall paths were exercised on the Air against
a sandboxed plist path and a fake `launchctl` (the same shape of override
`setup-machine.sh` already uses), with `plutil -lint` on the generated plist.
