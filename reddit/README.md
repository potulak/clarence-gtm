# The Reddit loop

Finds people talking about keeping in touch, ranks them, and hands you the few
worth answering. It never posts. You read, you write, you post by hand.

## Running it

    python3 scratch/reddit/monitor.py                  # one pass, both lanes
    python3 scratch/reddit/monitor.py --lane launch    # just one lane
    python3 scratch/reddit/monitor.py --subs           # which subs actually pay off
    python3 scratch/reddit/monitor.py --find "how do you keep in touch with friends who moved"

Then read the queue in the cockpit: `python3 scratch/cockpit/server.py`.

## How it works

Three stages, deliberately separated, because that is what lets the watchlist grow
without the bill growing with it.

1. **Collect** (free, unbounded). old.reddit takes many subreddits in one URL, on
   the search path too, so 80 subs cost about 20 fetches. Everything found is
   filed in the store.
2. **Rank** (free). Arithmetic over things already known: theme match, freshness,
   how much room there is to be useful, and what that subreddit has been worth.
3. **Judge** (costs money, capped). The model reads only the top slice, 60 posts
   for conversations and 30 for launch, and never reads the same post twice.

The queue is a **view**, not a run's output: the top 10% of everything kept that
you have not acted on. A thread found on Tuesday is still there on Friday.

## The loop that closes

Marking a post **posted** or **skip** writes into the store, and that teaches each
subreddit's worth. Your behaviour outweighs the model's opinion, with confidence
that grows as you act more, so subreddits you post in earn more of the next run's
judging budget. That is the only part of this nothing can generate for you.

## The second loop: your voice

The drafter is told who you are in prose, which is the weak way to ask for a
voice. A model matches a voice from examples of it far better than from any
description of one, so the writing you actually do is kept and fed back.

Every step of writing one reply is recorded with its text, in the `voice` table:
the angle you dictated, the model's draft, each revision you asked for, every
edit you made by hand, and what was in the box when you marked it posted.

Only one of those is evidence of how you write. A draft you sent untouched says
you approved it, which is worth knowing and is not a sample of your writing:
train on those and the model learns to imitate itself. So the examples come from
replies you **changed**, and the pair (what it offered, what you sent instead) is
the richest form, because the deletions carry as much voice as the words.

    python3 reddit/collector/voice.py             # where the set stands
    python3 reddit/collector/voice.py --examples  # the block the drafter gets
    python3 reddit/collector/voice.py --export    # jsonl, for a later fine-tune

Fifteen is the number to aim for. Three is where the drafter starts using them at
all, because one example reads as a template to be filled in rather than a voice.
Eight is where the generic tone goes. Past about thirty the prompt stops getting
better and only gets longer, which is why there is a cap and not a dial.

The seven replies that went out before any of this existed are mostly lost: the
card kept one text box per thread and overwrote it, so what survives cannot say
whether it was the model's sentence or yours. `--backfill` recovers five of them
marked `unknown`, which keeps them out of the examples on purpose.

## Two lanes

`watch.json` holds both, because they are opposite jobs.

- **conversations**: people with the problem. You help. You rarely mention the app.
- **launch**: places where showing what you built is welcome. Every card carries
  that subreddit's rule, because getting it wrong is how a first post gets you
  banned. `post_here` lists the subs where posting your own project IS the point;
  those are not a feed, so nothing watches them.

## Always-on machine

The code has to physically get there. Two halves arrive by different routes,
because `scratch/` is in .gitignore: the repo comes from git, and the engine
(which lives under scratch/) comes from rsync.

    # 0. on that Mac: System Settings > General > Sharing > Remote Login = on
    # 1. on that Mac, get the repo:
    git clone https://github.com/potulak/clarence.git Documents/clarence
    # 2. back here, send the parts git does not carry:
    bash scratch/reddit/sync-to.sh you@thatmac.local --with-data   # first time only
    # 3. on that Mac, put your API key in place, then:
    bash scratch/reddit/setup-machine.sh

`sync-to.sh` checks both of those first and tells you which one is missing rather
than copying half a system into place.

Later, when the code here changes, just step 2 again WITHOUT `--with-data`.

That installs the collector hourly and the cockpit as a service on the network, so
you read it from any Mac in the house. Afterwards run `sync-to.sh` WITHOUT
`--with-data`: sending the store again would overwrite every decision that machine
has recorded since, which is the one thing here that cannot be rebuilt.

Turn the collector off wherever you are no longer collecting, or two machines
build two different piles:

    launchctl unload ~/Library/LaunchAgents/app.clarence.reddit.hourly.plist

## Looking at the data from another Mac

The board is the live view, at `http://your-mac.local:4360`. Use the
name rather than the IP, which is a DHCP lease and moves.

For anything the board does not show, pull a snapshot and query it:

    bash scratch/reddit/pull-data.sh
    sqlite3 scratch/reddit/snapshot.db 'select subreddit, title, why from posts where keep=1'

It never syncs back. There is exactly one live store and it lives on the machine
doing the collecting.

## Working while the always-on Mac is shut

    bash scratch/reddit/offline.sh        # opens the same board here, on :4361

It serves the cockpit against your local snapshot. Read the queue, skip things,
mark things posted. Every decision is journalled locally. When that Mac is awake
again:

    bash scratch/reddit/push-actions.sh

Why this is safe rather than a split brain: the two machines write to different
columns. The collector owns `pre_score`, `llm_score`, `keep`, `why`, `last_seen`.
You own `status`, `status_reason`, `status_note`. They never contend. Replay is
last-write-wins by the time a decision was MADE, so a queued action can never
overwrite a newer one, and pushing twice does nothing the second time.

What offline mode deliberately cannot do is collect or judge. Those belong to the
one machine that owns the store, so there is only ever one of it.

`pull-data.sh` refuses to run while you have unpushed decisions, because
overwriting the snapshot would destroy the only copy of them.

## Who replied to you

    bash scratch/reddit/set-inbox-feed.sh    # once, from the clipboard
    python3 scratch/reddit/replies.py        # fetch (the hourly job does this)
    python3 scratch/reddit/replies.py --list # read them in the terminal

Reddit gives every account a private RSS feed of **comment replies only**, at
reddit.com/prefs/feeds. That is one request that returns exactly the replies
addressed to you, already attributed, with no API app to register.

Why not scrape the threads: old.reddit login-walls logged-out reads, and the
per-thread `.rss` that still answers rate-limits to about one request before
429ing. A dozen posted threads would be a dozen throttled requests that still
could not tell a reply to YOU from any other new comment.

The feed URL is a bearer credential for your inbox. `set-inbox-feed.sh` takes it
from the clipboard so it is never typed or pasted anywhere it could be logged,
and stores it in `reddit_creds.json`, which is gitignored. Changing your Reddit
password invalidates it; that surfaces as a clear message, and re-running the
setup script fixes it.

Replies show at the TOP of the phone board, above the queue, because a thread you
already posted in has left the queue and those are exactly the ones that get
replies. Each one also files its author into `people` as `replied-to-you`, since
someone who answered you is the warmest possible person to ask to try the app.

**Set it on the machine that collects**, which is the Air. If you run the setup
on the laptop instead, copy `reddit_creds.json` over, or the hourly job will keep
saying no feed is set.

## Files

- `monitor.py` collect, rank, judge, publish the queue
- `store.py` the record: every post seen, every verdict, every decision of yours
- `watch.json` the two lanes: subs, themes, posting rules
- `replies.py` who answered your comments, from the private replies feed
- `reddit.db` the store. Not in git, and not replaceable.
