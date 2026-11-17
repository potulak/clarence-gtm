-- Clarence Reddit cockpit, cloud half.
--
-- The split this preserves: the Mac owns POSTS AND VERDICTS, the cloud owns
-- YOUR DECISIONS. That is the same two-column invariant the two laptops already
-- use, with the cloud standing where the MacBook Pro used to. Neither side ever
-- writes the other's columns, so there is nothing to reconcile.
--
-- `queue` is a projection, not a record. It is whatever the last push computed,
-- and it can be dropped and rebuilt from the Mac at any time. The only rows here
-- that cannot be regenerated are in `actions`.

CREATE TABLE IF NOT EXISTS queue (
  id              TEXT PRIMARY KEY,
  rank            INTEGER,
  lane            TEXT,
  subreddit       TEXT,
  title           TEXT,
  url             TEXT,
  snippet         TEXT,
  author          TEXT,
  age_hours       REAL,
  num_comments    INTEGER,
  score           INTEGER,
  fit             REAL,
  llm_score       REAL,
  why             TEXT,
  post_rule       TEXT,
  matched_cluster TEXT,
  matched_phrase  TEXT,
  found_via       TEXT,
  source          TEXT,
  first_seen      TEXT,
  is_new          INTEGER,
  stale           INTEGER,
  -- Can you comment on it at all. Three states, so NO DEFAULT: 1 yes, 0 no,
  -- NULL nobody has told us. `archived` is Reddit's age lock, `locked` is a mod
  -- closing the thread, and both answer the one question the reply queue asks.
  -- Only the collector's OAuth listing can fill them; a laptop collecting over
  -- HTML leaves them NULL, and NULL has to read as "still worth trying".
  archived        INTEGER,
  locked          INTEGER
);

CREATE INDEX IF NOT EXISTS queue_rank ON queue(rank);

-- Append only, exactly like the Mac's journal, so the same last-write-wins
-- replay works in both directions and pushing twice is a no-op.
CREATE TABLE IF NOT EXISTS actions (
  at      TEXT NOT NULL,
  post_id TEXT NOT NULL,
  status  TEXT,
  reason  TEXT,
  note    TEXT,
  origin  TEXT
);

CREATE INDEX IF NOT EXISTS actions_post ON actions(post_id, at);
CREATE INDEX IF NOT EXISTS actions_at   ON actions(at);

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

-- Replies to your comments, pushed up from the Mac alongside the queue.
-- Also a projection: the Mac's `replies` table is the record, this is the copy
-- the phone reads. Deliberately NOT joined to `queue`, because a thread you have
-- already posted in has left the queue, and those are exactly the ones that get
-- replies.
CREATE TABLE IF NOT EXISTS replies (
  id           TEXT PRIMARY KEY,
  post_id      TEXT,
  subreddit    TEXT,
  author       TEXT,
  body         TEXT,
  url          TEXT,
  created_at   TEXT,
  post_title   TEXT,
  -- Added 2026-10-17 with the real board. See the note further down about why
  -- the Worker applies these to the live database rather than this file.
  my_comment   TEXT,
  post_url     TEXT,
  post_snippet TEXT,
  asked_at     TEXT,
  ask_outcome  TEXT,
  acknowledged INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS replies_when ON replies(created_at);

-- ---------------------------------------------------------------------------
-- The real board, 2026-10-17. Everything above served the stripped phone view:
-- a flat list of the whole queue. What the Mac board actually shows is the DAY'S
-- SHORTLIST, which is a different and much more selective thing, plus the people
-- worth going back to and the communities worth breaking into.
--
-- None of that is recomputed here. `shortlist.build` weighs the judge's verdict
-- against freshness and room to reply, caps how many come from one subreddit and
-- spends a slice of the day somewhere new; `new_communities` reads the whole
-- history of what each subreddit has been worth. Reimplementing either in SQL at
-- the edge would be two rankings that drift apart, and the phone would quietly
-- be showing a different day than the laptop. So the Mac computes, and these are
-- projections of the answer, exactly like `queue` above.
--
-- `queue` stays as it is, holding the full queue. The staleness watchdog reads
-- `/api/reddit` for `pushed_at`, `count` and `threads[].first_seen`, and that
-- contract is the alarm that found 45 dead days.

-- The day's list, ten to twenty posts, in the order the Mac ranked them.
CREATE TABLE IF NOT EXISTS picks (
  id             TEXT PRIMARY KEY,
  rank           INTEGER,         -- position in the day, not the Mac's score
  rank_score     REAL,            -- shortlist.rank, kept for the record
  tier           TEXT,            -- sure | maybe
  why_picked     TEXT,            -- "the judge said yes" | "somewhere new" | ...
  lane           TEXT,
  subreddit      TEXT,
  title          TEXT,
  url            TEXT,
  author         TEXT,
  snippet        TEXT,            -- the listing preview, often empty
  post_text      TEXT,            -- what the person actually wrote. See bodies.py
  comments       TEXT,            -- JSON array, when a thread read ever gets one
  age_hours      REAL,
  num_comments   INTEGER,
  score          INTEGER,
  score_at       TEXT,            -- when votes were last really checked
  fit            REAL,
  llm_score      REAL,
  why            TEXT,
  post_rule      TEXT,
  post_rule_says TEXT,
  first_seen     TEXT,
  created_utc    REAL,
  prospect       INTEGER,
  prospect_note  TEXT,
  asked_at       TEXT,            -- this author has already been asked
  status         TEXT,            -- as the Mac had it at push time
  draft          TEXT,            -- work in progress, from either side
  angle          TEXT,
  -- The same two as `queue`, and this is the copy the phone actually reads:
  -- the deck is drawn from `picks`. The phone has one pile rather than the
  -- board's two, so a closed thread is not hidden from the deck, it is marked
  -- on the card, before five minutes go into dictating an answer that cannot
  -- be posted.
  archived       INTEGER,
  locked         INTEGER
);

CREATE INDEX IF NOT EXISTS picks_rank ON picks(rank);

-- People worth going back to when there is something to show. The first-ten-users
-- list, and the reason the save button exists at all.
CREATE TABLE IF NOT EXISTS people (
  id            TEXT PRIMARY KEY,
  author        TEXT,
  subreddit     TEXT,
  title         TEXT,
  url           TEXT,
  snippet       TEXT,
  why           TEXT,
  status        TEXT,
  prospect_note TEXT,
  prospect_at   TEXT
);

-- Subreddits that keep producing posts the judge likes and have never been
-- posted in. Ground worth breaking into.
CREATE TABLE IF NOT EXISTS communities (
  subreddit TEXT PRIMARY KEY,
  n         INTEGER,
  good      INTEGER,
  hit_rate  REAL,
  avg_score REAL
);

-- Everything the founder decides here that is NOT a post status.
--
-- `actions` above holds statuses, because a status is one word per post and
-- replays through one function. These do not: asking a PERSON to try the app,
-- what came back, clearing a reply, saving someone. Each replays into a
-- different place in the Mac's store, so each carries its own payload.
--
-- Append only, same as `actions`, and pulled down by the same watermark. This is
-- the second half of the only irreplaceable data up here: without it, tapping
-- "I sent it" on the phone is a light that goes on and nothing more.
CREATE TABLE IF NOT EXISTS decisions (
  at      TEXT NOT NULL,
  kind    TEXT NOT NULL,   -- ask_sent | ask_outcome | reply_ack | prospect | event
  subject TEXT,            -- a post id, a reply id, or a reddit handle
  payload TEXT,            -- JSON, shaped per kind
  origin  TEXT
);

CREATE INDEX IF NOT EXISTS decisions_at ON decisions(at);

-- A draft in progress, which is neither a decision nor a projection: it is work
-- the founder has started and has not finished. It must survive a phone locking
-- itself mid-sentence, and it must reach the laptop, or dictating an angle on the
-- train and finishing it at the desk does not work.
--
-- Keyed by post, last write wins. Pulled down into the Mac's own state file.
CREATE TABLE IF NOT EXISTS drafts (
  post_id   TEXT PRIMARY KEY,
  draft     TEXT,
  angle     TEXT,
  post_text TEXT,
  at        TEXT
);

CREATE INDEX IF NOT EXISTS drafts_at ON drafts(at);

-- Replies need more than the queue push once gave them: the founder's own
-- comment is the other half of the conversation and the drafter writes an
-- opening line at someone mid-thread without it.
--
-- Added as columns on `replies` rather than a new table, because they travel
-- together and arrive together.
--
-- SQLite has no ADD COLUMN IF NOT EXISTS, so running this file against the live
-- database would fail on the second column every time after the first. The
-- Worker does this migration itself instead, once, on first use: see
-- `ensureSchema` in src/worker.js. Listed here because this file is still the
-- description of the shape, and a database created from it should already have
-- them.
--
--   replies.my_comment    what he said in that thread, which the drafter needs
--   replies.post_url      the thread, so "open the reply" has somewhere to go
--   replies.post_snippet  enough of the post to reply in context
--   replies.asked_at      this person has already been asked to try it
--   replies.ask_outcome   sent | replied | signed_up | ignored
--   replies.acknowledged  dealt with, so the inbox is what is still owed
--
-- The same is true of the two columns added to `queue` and `picks` on
-- 2026-10-18 (`archived`, `locked`): `ensureSchema` adds them to a live
-- database, and they are written into the shapes above so that one created from
-- this file already has them.
