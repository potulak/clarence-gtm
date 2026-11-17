#!/usr/bin/env python3
"""The record: every post we have ever seen, and what happened to it.

Why a store and not another JSON file. Watching a handful of subreddits, you can
rebuild the world on every run and throw it away. Watching hundreds, two things
break. First, cost: judging every post with a model is linear in posts, and at
that scale most posts are not worth a model's attention. Second, memory: without
somewhere to accumulate, there is no "list" to prioritise, only whatever this
run happened to catch, and a good thread you did not answer on Tuesday is gone by
Wednesday.

So collection writes EVERYTHING here, cheaply. Ranking happens in two stages: a
free score over everything, then the model only on the slice that free score puts
at the top. The queue you actually read is a VIEW over this table, the best of
what you have not acted on yet.

And it is what makes the loop close. Every verdict, and more importantly every
decision you make (posted, skipped), is written back here, which is what teaches
the free score which communities are worth the model's time next run.

Stdlib only. One file: reddit.db, alongside this script.
"""
from __future__ import annotations

import datetime
import os
import json
import sqlite3
from pathlib import Path

def _data_dir() -> Path:
    """Where the live data sits, which is never where this code sits.

    This module is version controlled now, so that the scheduled runner can
    import it. The data is not, and must not be: the store holds real people's
    posts, and `reddit_creds.json` sits beside it. So code and data part company
    here.

    On a laptop the answer is the directory the store has always been in,
    `scratch/reddit`, worked out from this file's position rather than from a
    variable someone has to remember to set. That is deliberate: a missing env
    var would otherwise resolve to an empty database next to the code, and a
    fresh empty store is the one failure that looks exactly like a quiet day
    while it re-judges everything you have already paid to judge.

    A runner has no `scratch/`, so it sets CLARENCE_REDDIT_DATA explicitly.
    """
    env = os.environ.get("CLARENCE_REDDIT_DATA")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    if here.name == "collector":          # reddit/collector -> <repo>/scratch/reddit
        return here.parents[1] / "scratch" / "reddit"
    return here                           # still running from the old location


DATA = _data_dir()

# Overridable so you can point analysis at a snapshot pulled off the always-on
# machine without touching, or being confused with, a live store.
DB = Path(os.environ.get("REDDIT_DB") or DATA / "reddit.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id             TEXT PRIMARY KEY,
  lane           TEXT NOT NULL,
  subreddit      TEXT,
  title          TEXT,
  url            TEXT,
  snippet        TEXT,
  age_hours      REAL,
  num_comments   INTEGER,
  score          INTEGER,
  found_via      TEXT,
  matched_cluster TEXT,
  matched_phrase TEXT,
  post_rule      TEXT,
  first_seen     TEXT,
  last_seen      TEXT,
  pre_score      REAL,
  llm_score      REAL,
  keep           INTEGER,
  why            TEXT,
  judged_at      TEXT,
  status         TEXT DEFAULT 'todo',
  acted_at       TEXT
);
CREATE INDEX IF NOT EXISTS posts_lane_pre  ON posts(lane, pre_score DESC);
CREATE INDEX IF NOT EXISTS posts_unjudged  ON posts(lane, llm_score, pre_score DESC);
CREATE INDEX IF NOT EXISTS posts_sub       ON posts(subreddit);
CREATE TABLE IF NOT EXISTS spend (
  at         TEXT,
  lane       TEXT,
  model      TEXT,
  in_tokens  INTEGER,
  out_tokens INTEGER,
  posts      INTEGER,
  usd        REAL
);
CREATE INDEX IF NOT EXISTS spend_at ON spend(at);
CREATE TABLE IF NOT EXISTS notes (k TEXT PRIMARY KEY, v TEXT, at TEXT);
-- People, not posts. A thread is one person asking and often five more in the
-- comments saying "same" in their own words. Those five are the same audience
-- and normally nobody writes them down.
CREATE TABLE IF NOT EXISTS people (
  author    TEXT,
  post_id   TEXT,
  subreddit TEXT,
  kind      TEXT,          -- op | commenter
  quote     TEXT,
  url       TEXT,
  first_at  TEXT,
  saved     INTEGER DEFAULT 0,
  note      TEXT,
  PRIMARY KEY (author, post_id)
);
CREATE INDEX IF NOT EXISTS people_saved ON people(saved);
-- Every decision you make, appended, whichever machine you made it on. This is
-- what lets you work the queue on a laptop while the always-on Mac is shut, then
-- hand the decisions back when it wakes. Two writers are safe here because they
-- touch different columns: the collector owns pre_score / llm_score / keep / why,
-- and you own status / status_reason / status_note. They never contend.
CREATE TABLE IF NOT EXISTS actions (
  at      TEXT,
  post_id TEXT,
  status  TEXT,
  reason  TEXT,
  note    TEXT,
  origin  TEXT,
  pushed  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS actions_unpushed ON actions(pushed, at);
-- Every step of writing one reply, with the text, not just its shape.
--
-- `events` already records that a draft happened and how many characters it ran
-- to. That is the right thing for measuring the workflow and the wrong thing for
-- learning a voice, because the one part that cannot be regenerated is the words.
-- Seven replies went out before this table existed and the record of them is
-- seven rows saying "posted": the drafts survived only because the card happened
-- to keep the last value of its text box, and nothing says whether that value is
-- what the model wrote or what he rewrote it into.
--
-- So: append one row per step. The model's draft, each revision, every edit he
-- makes by hand, and the text in the box at the moment he marks it posted. The
-- pair that matters is (what the model said, what he sent instead), because the
-- deletions carry as much voice as the words that stay.
CREATE TABLE IF NOT EXISTS voice (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT,
  post_id     TEXT,
  step        TEXT,     -- angle | model | revise | edit | final
  text        TEXT,
  author      TEXT,     -- which persona it was written as
  context     TEXT,     -- new_comment | reply_comment | reply_to_reply | seed_question
  instruction TEXT,     -- for a revise, what was asked for
  subreddit   TEXT,
  from_id     INTEGER,  -- the voice row this text was derived from
  provenance  TEXT,     -- his | model | unknown
  origin      TEXT
);
CREATE INDEX IF NOT EXISTS voice_post ON voice(post_id, id);
CREATE INDEX IF NOT EXISTS voice_step ON voice(step, provenance);
"""

# Anthropic list prices, dollars per million tokens. Sonnet 5 is on introductory
# pricing ($2/$10) through 2026-08-31, so the real bill is lower than this until
# then. Rounding up is the honest way to quote a running cost.
# Per million tokens, (input, output). Checked against the model table on
# 2026-10-17: Sonnet 5 had been recorded at the older 3.00/15.00 and the current
# Haiku was missing entirely, so every spend figure for a Haiku run would have
# been priced as Haiku 4.5 and come out ten times too high.
PRICES = {
    "claude-sonnet-5":   (2.00, 10.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-5-5":  (0.10,  0.50),
    "claude-haiku-4-5":  (1.00,  5.00),
    "claude-opus-5":     (5.00, 25.00),
    "claude-opus-5-5":   (4.00, 20.00),
}


def price(model: str, in_tok: int, out_tok: int) -> float:
    pin, pout = PRICES.get(model, PRICES["claude-sonnet-5"])
    return (in_tok * pin + out_tok * pout) / 1_000_000


def record_spend(con, lane: str, model: str, in_tok: int, out_tok: int,
                 posts: int) -> float:
    usd = price(model, in_tok, out_tok)
    con.execute("INSERT INTO spend (at, lane, model, in_tokens, out_tokens, posts, usd) "
                "VALUES (?,?,?,?,?,?,?)", (now(), lane, model, in_tok, out_tok, posts, usd))
    return usd


def spend_summary(con) -> dict:
    """What this loop has actually cost, and what it is on course to cost.

    Projected from the last 7 days rather than all time, because the first runs
    judge a backlog and are not what a steady hour looks like.
    """
    import datetime as _dt
    n = _dt.datetime.now(_dt.timezone.utc)
    def since(days):
        cut = (n - _dt.timedelta(days=days)).isoformat()
        r = con.execute("SELECT COALESCE(SUM(usd),0) usd, COALESCE(SUM(posts),0) posts, "
                        "COUNT(*) calls FROM spend WHERE at >= ?", (cut,)).fetchone()
        return {"usd": round(r["usd"], 4), "posts": r["posts"], "calls": r["calls"]}
    day, week = since(1), since(7)
    total = con.execute("SELECT COALESCE(SUM(usd),0) usd, COALESCE(SUM(posts),0) posts, "
                        "MIN(at) first FROM spend").fetchone()
    # Only project once there is a real day of data behind it.
    hours = 0
    if total["first"]:
        first = _dt.datetime.fromisoformat(total["first"])
        hours = max(1.0, (n - first).total_seconds() / 3600)
    per_hour = (week["usd"] / min(hours, 168)) if hours else 0
    by_model = [dict(r) for r in con.execute(
        "SELECT model, COUNT(*) calls, SUM(usd) usd FROM spend GROUP BY model").fetchall()]
    return {"today": day, "week": week,
            "total_usd": round(total["usd"], 4), "total_posts": total["posts"],
            "hours_running": round(hours, 1),
            "per_hour_usd": round(per_hour, 4),
            "projected_month_usd": round(per_hour * 24 * 30, 2),
            "by_model": by_model}


def now() -> str:
    # Microseconds, not seconds. These timestamps decide which of two machines'
    # decisions wins, and at second resolution two edits in the same second tie,
    # which let a stale replay overwrite a newer decision. Caught by the merge
    # test, which is exactly what that test is for.
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


# Why you skipped, not just that you did. A bare skip says "something was wrong",
# which the ranking cannot act on. These say WHAT was wrong, and they mean
# different things: "wrong subject" is a fact about the subreddit and should count
# against it, while "too old" is a fact about timing and should not.
SKIP_REASONS = {
    "deleted":       "post was deleted or removed",
    "off-subject":   "not about keeping in touch",
    "too-late":      "too old, or already well answered",
    "nothing-to-add": "nothing useful I could say",
    "wrong-crowd":   "not a community I belong in",
    "would-be-ad":   "replying here would read as advertising",
    "not-my-place":  "too raw, not mine to answer",
}
# Only these tell you something about the COMMUNITY. The others are about the
# moment or about you, and holding them against a subreddit would be wrong.
SUB_FAULT = {"off-subject", "wrong-crowd", "would-be-ad"}


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(posts)")}
    if "author" not in cols:
        # Who wrote it. "This person really feels the problem" is only useful if
        # you can find them again, and a post id is not a person.
        con.execute("ALTER TABLE posts ADD COLUMN author TEXT")
        # A prospect is orthogonal to what you DID about the thread: you can post
        # a reply and also mark them worth going back to.
        con.execute("ALTER TABLE posts ADD COLUMN prospect INTEGER DEFAULT 0")
        con.execute("ALTER TABLE posts ADD COLUMN prospect_note TEXT")
        con.execute("ALTER TABLE posts ADD COLUMN prospect_at TEXT")
        con.commit()
    if "created_utc" not in cols:
        # Age was stored as a NUMBER OF HOURS computed at collection time, so a
        # post found at 12h old still read "12.4h old" six days later. Age is
        # derived, not a fact: store WHEN it was written and work it out on the
        # way to the screen.
        con.execute("ALTER TABLE posts ADD COLUMN created_utc REAL")
        con.execute("UPDATE posts SET created_utc = "
                    "strftime('%s', first_seen) - (COALESCE(age_hours,0) * 3600) "
                    "WHERE created_utc IS NULL AND first_seen IS NOT NULL")
        con.commit()
    if "status_reason" not in cols:
        con.execute("ALTER TABLE posts ADD COLUMN status_reason TEXT")
        con.execute("ALTER TABLE posts ADD COLUMN status_note TEXT")
        con.commit()
    if "post_text" not in cols:
        # What the person actually wrote. `snippet` is the listing preview and is
        # capped at 280 characters, where it exists at all: the listing path the
        # collector uses carries no body whatsoever, so most posts arrive as a
        # title. This column is filled in afterwards by `bodies.py` from the
        # subreddit's own feed, which is the only logged-out path that still
        # returns a real post. Separate from `snippet` on purpose, so a sync that
        # trades snippets can never overwrite a full body with a preview.
        con.execute("ALTER TABLE posts ADD COLUMN post_text TEXT")
        con.execute("ALTER TABLE posts ADD COLUMN post_text_at TEXT")
        con.commit()
    if "archived" not in cols:
        # Whether the thread will take a comment at all. Both come off the OAuth
        # listing, which has carried them on every post all along, and they mean
        # different things: `archived` is Reddit's own age lock, which closes
        # comments AND votes on old posts in the subs whose mods leave archiving
        # switched on, while `locked` is a moderator (or the author) closing one
        # thread, which stops new comments and can be lifted again. Different
        # reasons, one answer to the only question the reply queue asks.
        #
        # NO DEFAULT, which is the whole point of these two columns. NULL means
        # nobody has told us, which is all the HTML collector on a laptop can
        # ever say, and unknown has to stay answerable. A DEFAULT 0 would turn
        # every post already in the store, and every post a laptop collects
        # after this, into a claim that you can reply to it: the bug of
        # 2026-10-18 arriving from the other direction.
        con.execute("ALTER TABLE posts ADD COLUMN archived INTEGER")
        con.execute("ALTER TABLE posts ADD COLUMN locked INTEGER")
        con.commit()
    # Every step of the review, not just the verdict. `actions` holds the final
    # status and is what syncs between machines; this is the trail that led
    # there, and it is the only place the REASONS live alongside what the post
    # actually looked like. That pairing is the training set: post features ->
    # what the human did about it.
    con.execute("""CREATE TABLE IF NOT EXISTS events (
        at        TEXT NOT NULL,
        post_id   TEXT,
        kind      TEXT NOT NULL,
        detail    TEXT,
        origin    TEXT
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS events_post ON events(post_id, at)")
    # Post ids a listing has handed us, whether or not they were worth filing.
    # This is what lets a run read /new only as far back as it needs to. See
    # read_ids for why the posts table cannot answer that question.
    con.execute("CREATE TABLE IF NOT EXISTS reads ("
                " id TEXT PRIMARY KEY,"
                " at TEXT NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS reads_at ON reads(at)")
    con.execute("CREATE INDEX IF NOT EXISTS events_kind ON events(kind, at)")
    # Who has been asked to try the thing, and what came of it. One row per ask,
    # keyed on the person rather than the thread.
    con.execute("""CREATE TABLE IF NOT EXISTS asks (
        at       TEXT NOT NULL,
        author   TEXT NOT NULL,
        post_id  TEXT,
        reply_id TEXT,
        channel  TEXT,
        text     TEXT,
        outcome  TEXT DEFAULT 'sent'
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS asks_author ON asks(author)")
    con.commit()
    return con


def log_event(con: sqlite3.Connection, post_id: str | None, kind: str,
              detail: dict | None = None, origin: str = "board") -> None:
    """Record one step of the workflow. Never raises: losing a measurement must
    never cost the action it was measuring."""
    try:
        con.execute("INSERT INTO events (at, post_id, kind, detail, origin) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now(), post_id, kind,
                     json.dumps(detail, ensure_ascii=False) if detail else None,
                     origin))
        con.commit()
    except Exception:
        pass


# The steps whose text the MODEL produced, as against the ones he typed. Used to
# work out, for a finished reply, whether he changed anything at all: a draft he
# accepted untouched tells you he approved it, which is worth knowing and is not
# the same as a sample of his writing.
_MODEL_STEPS = {"model", "revise"}


def last_voice(con: sqlite3.Connection, post_id: str,
               steps: set[str] | None = None) -> sqlite3.Row | None:
    """The most recent captured step for a thread, optionally of certain kinds."""
    q = "SELECT * FROM voice WHERE post_id = ?"
    args: list = [post_id]
    if steps:
        q += " AND step IN (%s)" % ",".join("?" * len(steps))
        args += sorted(steps)
    q += " ORDER BY id DESC LIMIT 1"
    try:
        return con.execute(q, args).fetchone()
    except Exception:
        return None


def capture(con: sqlite3.Connection, post_id: str | None, step: str,
            text: str | None, author: str | None = None,
            context: str | None = None, instruction: str | None = None,
            subreddit: str | None = None, provenance: str | None = None,
            force: bool = False) -> int | None:
    """Keep the words from one step of writing a reply.

    Two things it deliberately refuses to do. It never stores an empty box, and
    it never stores text identical to the last thing captured for that thread,
    because the draft box saves on every blur and clicking a button blurs it: a
    naive capture would fill the table with copies of one sentence and make ten
    examples look like ninety.

    `force` exists for exactly one case, the moment a reply is marked posted. The
    text there is often identical to the draft, because sometimes a draft is good
    and goes out untouched, and that still has to be written down: a reply with
    no final row looks like a reply that was never sent. So the final row is
    always written, and whether he changed anything is then a comparison, not an
    absence.

    Never raises. Capture is bookkeeping, and bookkeeping must not be able to
    cost you the reply it was recording.
    """
    body = (text or "").strip()
    if not body or not post_id:
        return None
    try:
        prev = last_voice(con, post_id)
        if (not force and prev is not None
                and (prev["text"] or "").strip() == body):
            return prev["id"]
        if provenance is None:
            provenance = "model" if step in _MODEL_STEPS else "his"
        cur = con.execute(
            "INSERT INTO voice (at, post_id, step, text, author, context, "
            "instruction, subreddit, from_id, provenance, origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now(), post_id, step, body, author, context, instruction, subreddit,
             prev["id"] if prev is not None else None, provenance,
             os.environ.get("CLARENCE_ORIGIN", "local")))
        con.commit()
        return cur.lastrowid
    except Exception:
        return None


def training_rows(con: sqlite3.Connection) -> list[dict]:
    """Every judged post paired with what you did about it, ready to learn from.

    One row per post that has a decision: the features that were on screen when
    you decided (the model's score, the pre-score, the subreddit, the age, the
    Reddit score) and your verdict, with the skip reason when there is one.
    """
    return [dict(r) for r in con.execute("""
        SELECT p.id, p.subreddit, p.title, p.lane, p.llm_score, p.pre_score,
               p.score AS reddit_score, p.num_comments, p.age_hours,
               p.found_via, p.matched_cluster, p.prospect, p.prospect_note,
               p.status, p.status_reason, p.status_note, p.acted_at,
               (SELECT COUNT(*) FROM events e
                 WHERE e.post_id = p.id AND e.kind = 'revise') AS revisions
          FROM posts p
         WHERE p.status IN ('posted', 'skip', 'drafted')
           AND p.llm_score IS NOT NULL
         ORDER BY p.acted_at DESC""")]


# ------------------------------------------------------------------ writing
FIELDS = ("lane", "subreddit", "author", "title", "url", "snippet", "age_hours", "created_utc",
          "num_comments", "score", "found_via", "matched_cluster",
          "matched_phrase", "post_rule", "pre_score",
          # Three-state, and a row that does not carry them inserts NULL, which
          # is the HTML collector saying nothing rather than saying no.
          "archived", "locked")


def upsert(con: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    """File what we just collected. Returns (new, seen again).

    A post we already hold keeps its verdict and, above all, keeps YOUR status.
    Only the things that genuinely change are refreshed: reply count, age, and
    the free score.
    """
    stamp, fresh, again = now(), 0, 0
    for r in rows:
        vals = [r.get(f) for f in FIELDS]
        cur = con.execute("SELECT 1 FROM posts WHERE id = ?", (r["id"],))
        if cur.fetchone():
            con.execute(
                "UPDATE posts SET num_comments = ?, age_hours = ?, pre_score = ?, "
                # COALESCE both ways round the two sources. A listing that knows
                # overwrites what we held, because a mod can lock a thread after
                # we filed it and can unlock it again, so True must be able to
                # become False. A collector that does not know passes NULL and
                # keeps what is there, so a laptop run cannot erase what the
                # runner learned.
                "archived = COALESCE(?, archived), locked = COALESCE(?, locked), "
                "last_seen = ? WHERE id = ?",
                (r.get("num_comments"), r.get("age_hours"), r.get("pre_score"),
                 r.get("archived"), r.get("locked"),
                 stamp, r["id"]))
            again += 1
        else:
            con.execute(
                f"INSERT INTO posts (id, {', '.join(FIELDS)}, first_seen, last_seen) "
                f"VALUES (?{', ?' * (len(FIELDS) + 2)})",
                [r["id"], *vals, stamp, stamp])
            fresh += 1
    con.commit()
    return fresh, again


def record_verdict(con: sqlite3.Connection, pid: str, score: float,
                   keep: bool, why: str) -> None:
    con.execute("UPDATE posts SET llm_score = ?, keep = ?, why = ?, judged_at = ? "
                "WHERE id = ?", (score, 1 if keep else 0, why, now(), pid))


def set_status(con: sqlite3.Connection, pid: str, status: str,
               reason: str | None = None, note: str | None = None) -> None:
    """Your decision. This is the half of the loop the machine cannot produce."""
    stamp = now()
    con.execute("UPDATE posts SET status = ?, acted_at = ?, "
                "status_reason = COALESCE(?, status_reason), "
                "status_note = COALESCE(?, status_note) WHERE id = ?",
                (status, stamp, reason, note, pid))
    con.execute("INSERT INTO actions (at, post_id, status, reason, note, origin, pushed) "
                "VALUES (?,?,?,?,?,?,0)",
                (stamp, pid, status, reason, note, os.environ.get("CLARENCE_ORIGIN", "local")))
    con.commit()


def unpushed(con: sqlite3.Connection) -> list[dict]:
    """Decisions made here that the always-on machine has not been told about."""
    return [dict(r) for r in con.execute(
        "SELECT rowid, at, post_id, status, reason, note FROM actions "
        "WHERE pushed = 0 ORDER BY at")]


def mark_pushed(con: sqlite3.Connection, rowids: list[int]) -> None:
    con.executemany("UPDATE actions SET pushed = 1 WHERE rowid = ?",
                    [(r,) for r in rowids])
    con.commit()


def apply_action(con: sqlite3.Connection, post_id: str, status: str,
                 reason: str | None, note: str | None, at: str) -> bool:
    """Replay one decision from another machine.

    Last write wins, by the time the decision was MADE rather than the time it
    arrived, so a stale queued action never overwrites a newer one you made here.
    """
    row = con.execute("SELECT acted_at FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        return False                         # a post this machine never collected
    if row["acted_at"] and row["acted_at"] >= at:
        # Already decided here, at least as recently. On a genuine tie, keep what
        # this machine already has rather than let a replay churn it.
        return False
    con.execute("UPDATE posts SET status = ?, acted_at = ?, "
                "status_reason = COALESCE(?, status_reason), "
                "status_note = COALESCE(?, status_note) WHERE id = ?",
                (status, at, reason, note, post_id))
    con.commit()
    return True


# ------------------------------------------------------------------ reading
# A thread you cannot usefully reply to is not worth paying to judge. Three days
# is where a reply stops landing: the conversation has moved on and a comment
# arrives under thirty others.
ANSWERABLE_HOURS = 72


def unjudged(con: sqlite3.Connection, lane: str, limit: int) -> list[dict]:
    """The slice worth spending the model on: still answerable first, then best
    free score.

    Ordering by pre_score alone spent the whole budget on the highest-scoring
    posts in the store regardless of age, so a run could judge six-week-old
    threads while that morning's arrivals waited. Age is not a tie-breaker here,
    it is the first sort key: an old post can be scored later for free, but a
    fresh one judged late is a reply that never gets written.
    """
    cur = con.execute(
        "SELECT * FROM posts WHERE lane = ? AND llm_score IS NULL "
        "ORDER BY (created_utc > strftime('%s','now') - ? * 3600) DESC, "
        "         pre_score DESC LIMIT ?",
        (lane, ANSWERABLE_HOURS, limit))
    return [dict(r) for r in cur.fetchall()]


def queue(con: sqlite3.Connection, lane: str | None = None,
          top_pct: float = 0.10, floor: int = 10, ceiling: int = 60) -> list[dict]:
    """The top slice of what you have not acted on yet.

    Ten per cent of a growing pile, with a floor so a quiet day still gives you
    something and a ceiling so a loud one does not give you a wall.
    """
    # A row with no title or link is not something you can act on. Older verdicts
    # carried over from before this store existed can look like that: keep them,
    # they still teach the sub priors, but never show them as a card.
    q = ("SELECT * FROM posts WHERE keep = 1 AND status IN ('todo', 'drafted') "
         "AND llm_score IS NOT NULL "
         "AND COALESCE(title, '') != '' AND COALESCE(url, '') != ''")
    args: list = []
    if lane:
        q += " AND lane = ?"
        args.append(lane)
    rows = [dict(r) for r in con.execute(q + " ORDER BY llm_score DESC, pre_score DESC",
                                         args).fetchall()]
    if not rows:
        return []
    if top_pct >= 1.0:
        return rows                      # "show me everything" means everything
    return rows[:max(floor, min(ceiling, round(len(rows) * top_pct)))]


def sub_priors(con: sqlite3.Connection, lane: str) -> dict[str, float]:
    """What each community has been worth, learned from the record.

    Two signals, and the second one matters far more: what the model kept, and
    what YOU actually posted to. A sub where you have posted is proven; a sub the
    model likes is only promising. Smoothed, so one lucky hit does not crown a
    subreddit and one dud does not bury it.
    """
    faults = "','".join(sorted(SUB_FAULT))
    cur = con.execute(
        "SELECT subreddit, COUNT(*) n, SUM(COALESCE(keep, 0)) kept, "
        "SUM(CASE WHEN status = 'posted' THEN 1 ELSE 0 END) posted, "
        # A skip counts against a subreddit only when the reason was ABOUT the
        # subreddit. Skipping because a thread went cold says nothing about the
        # community, and an unexplained skip is not evidence either way.
        f"SUM(CASE WHEN status = 'skip' AND status_reason IN ('{faults}') "
        "THEN 1 ELSE 0 END) skipped "
        "FROM posts WHERE lane = ? AND (llm_score IS NOT NULL OR status != 'todo') "
        "GROUP BY subreddit", (lane,))
    out = {}
    for r in cur.fetchall():
        n, kept, posted, skipped = r["n"], r["kept"] or 0, r["posted"] or 0, r["skipped"] or 0
        keep_rate = (kept + 1) / (n + 3)          # promising: what the model thinks
        acted = posted + skipped
        if not acted:
            out[(r["subreddit"] or "").lower()] = keep_rate
            continue
        act_rate = (posted + 1) / (acted + 2)     # proven: what you actually did
        # How much your behaviour should count grows with how much of it there is.
        # One post is an anecdote. Twenty is the answer, and it should then drown
        # out the model's opinion entirely.
        w = acted / (acted + 2)
        out[(r["subreddit"] or "").lower()] = (1 - w) * keep_rate + w * act_rate
    return out


def read_volume(con: sqlite3.Connection, lane: str) -> dict[str, float]:
    """How many posts each subreddit hands us on a typical run.

    This is how busy a subreddit IS, which is what decides how deep a group has
    to be read. It deliberately does not come from the `posts` table: a
    watchlist sub is only filed when a theme phrase matches, so r/AskReddit is
    one of the busiest places on Reddit and has filed almost nothing. Counting
    what we FILED would call it quiet and keep it grouped with the other giants,
    which is the bug this exists to fix.

    Smoothed across runs, so one burst does not redraw the groups.
    """
    try:
        raw = con.execute("SELECT v FROM notes WHERE k = ?",
                          (f"read_volume:{lane}",)).fetchone()
        return {k: float(v) for k, v in json.loads(raw["v"]).items()} if raw else {}
    except Exception:
        return {}


def record_read_volume(con: sqlite3.Connection, lane: str,
                       counts: dict[str, int], alpha: float = 0.3) -> None:
    """Fold this run's observed per-sub counts into the running average."""
    prev = read_volume(con, lane)
    for sub, n in counts.items():
        s = sub.lower()
        prev[s] = round((1 - alpha) * prev.get(s, float(n)) + alpha * n, 2)
    note(con, f"read_volume:{lane}", json.dumps(prev, sort_keys=True))


# How long to remember that we have SEEN a post id. Only needs to outlast the
# gap between runs, including the catch-up run after a night asleep, so three
# days is already generous. It is pruned on every write.
READS_TTL_DAYS = 3


def record_reads(con: sqlite3.Connection, ids: list) -> None:
    """Remember every post id a listing handed us, filed or not."""
    if not ids:
        return
    con.executemany("INSERT OR IGNORE INTO reads (id, at) VALUES (?, ?)",
                    [(i, now()) for i in ids])
    con.execute("DELETE FROM reads WHERE at < strftime('%Y-%m-%dT%H:%M:%S', "
                "'now', ?)", (f"-{READS_TTL_DAYS} days",))
    con.commit()


def read_ids(con: sqlite3.Connection) -> set:
    """Post ids we have already READ, which is what tells a walk back through
    /new when it has reached ground it covered on an earlier run.

    Deliberately not "ids in the posts table". A watchlist sub is only FILED
    when a theme phrase matches, so a page of r/AskReddit overlaps the posts
    table by 0 percent however many times we have read it, while the native
    subs overlap by 89 percent. Stopping on the posts table would therefore
    stop correctly for the native groups and never stop at all for the
    watchlist ones, walking to the page cap on every single run: four times the
    requests, to re-read the same listings.
    """
    return {r["id"] for r in con.execute("SELECT id FROM reads")}


def note(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO notes (k, v, at) VALUES (?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, at=excluded.at",
                (key, value, now()))
    con.commit()


def clear_note(con: sqlite3.Connection, key: str) -> None:
    con.execute("DELETE FROM notes WHERE k = ?", (key,))
    con.commit()


def health(con: sqlite3.Connection) -> dict:
    """Is this thing actually working?

    A loop that collects but cannot judge looks, from the queue, exactly like a
    quiet day on Reddit. It is not the same thing at all, and the difference is
    worth shouting about.
    """
    import datetime as _dt
    n = _dt.datetime.now(_dt.timezone.utc)
    row = con.execute("SELECT MAX(judged_at) j, MAX(first_seen) c FROM posts").fetchone()
    backlog = con.execute(
        "SELECT COUNT(*) c FROM posts WHERE llm_score IS NULL").fetchone()["c"]
    err = con.execute("SELECT v, at FROM notes WHERE k = 'judge_error'").fetchone()

    def hours(ts):
        return round((n - _dt.datetime.fromisoformat(ts)).total_seconds() / 3600, 1) if ts else None

    stale = hours(row["j"])
    return {"last_judged": row["j"], "hours_since_judged": stale,
            "last_collected": row["c"], "hours_since_collected": hours(row["c"]),
            "backlog": backlog,
            "judge_error": err["v"] if err else None,
            "judge_error_at": err["at"] if err else None,
            "ok": (err is None) and stale is not None and stale < 3}


def set_prospect(con: sqlite3.Connection, pid: str, on: bool,
                 note: str | None = None) -> None:
    """Mark someone as worth going back to. This is the first-ten-users list, and
    it is built out of people who described the problem in their own words."""
    con.execute("UPDATE posts SET prospect = ?, prospect_at = ?, "
                "prospect_note = COALESCE(?, prospect_note) WHERE id = ?",
                (1 if on else 0, now() if on else None, note, pid))
    con.commit()


def prospects(con: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT id, author, subreddit, title, url, snippet, why, status, "
        "prospect_note, prospect_at, created_utc FROM posts "
        "WHERE prospect = 1 ORDER BY prospect_at DESC")]


# Asking someone to try the thing is tracked against the PERSON, not the post.
# The same human turns up in three threads, and being pitched three times is how
# a good conversation becomes spam. The unit of "already asked" is u/them.
def record_ask(con: sqlite3.Connection, author: str, *, post_id: str | None = None,
               reply_id: str | None = None, channel: str = "thread",
               text: str | None = None) -> None:
    con.execute("INSERT INTO asks (at, author, post_id, reply_id, channel, text, "
                "outcome) VALUES (?,?,?,?,?,?,?)",
                (now(), (author or "").lstrip("/").removeprefix("u/").lower(),
                 post_id, reply_id, channel, text, "sent"))
    con.commit()


def asked_authors(con: sqlite3.Connection) -> dict[str, dict]:
    """Everyone already asked, newest ask per person."""
    out: dict[str, dict] = {}
    for r in con.execute("SELECT * FROM asks ORDER BY at"):
        out[r["author"]] = dict(r)
    return out


def set_ask_outcome(con: sqlite3.Connection, author: str, outcome: str) -> None:
    """What came back: replied, signed_up, ignored. Your answer is the only
    measurement of whether any of this works."""
    con.execute("UPDATE asks SET outcome = ? WHERE author = ?",
                (outcome, (author or "").lstrip("/").removeprefix("u/").lower()))
    con.commit()


def ask_stats(con: sqlite3.Connection) -> dict:
    rows = [dict(r) for r in con.execute(
        "SELECT outcome, COUNT(*) n FROM asks GROUP BY outcome")]
    by = {r["outcome"]: r["n"] for r in rows}
    return {"sent": sum(by.values()), "by_outcome": by,
            "people": con.execute(
                "SELECT COUNT(DISTINCT author) c FROM asks").fetchone()["c"]}


def ack_reply(con: sqlite3.Connection, reply_id: str, on: bool = True) -> None:
    """Mark a reply as dealt with, so the inbox shows what is still owed an answer.

    Reversible on purpose: acknowledging is a one-tap action on a phone and the
    wrong tap should cost nothing.
    """
    con.execute("UPDATE replies SET acknowledged = ? WHERE id = ?",
                (1 if on else 0, reply_id))
    con.commit()


def add_people(con: sqlite3.Connection, post_id: str, subreddit: str,
               rows: list[dict]) -> int:
    n = 0
    for r in rows:
        cur = con.execute("SELECT 1 FROM people WHERE author = ? AND post_id = ?",
                          (r["author"], post_id)).fetchone()
        if cur:
            continue
        con.execute("INSERT INTO people (author, post_id, subreddit, kind, quote, "
                    "url, first_at, saved) VALUES (?,?,?,?,?,?,?,0)",
                    (r["author"], post_id, subreddit, r.get("kind", "commenter"),
                     r.get("text", "")[:1200], r.get("url", ""), now()))
        n += 1
    con.commit()
    return n


def save_person(con: sqlite3.Connection, author: str, post_id: str,
                on: bool, note: str | None = None) -> None:
    con.execute("UPDATE people SET saved = ?, note = COALESCE(?, note) "
                "WHERE author = ? AND post_id = ?",
                (1 if on else 0, note, author, post_id))
    con.commit()


def people_for(con: sqlite3.Connection, post_id: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM people WHERE post_id = ? ORDER BY kind DESC, rowid", (post_id,))]


def saved_people(con: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM people WHERE saved = 1 ORDER BY first_at DESC")]


def search(con: sqlite3.Connection, q: str, limit: int = 60) -> dict:
    """Find it again. Across posts AND across what people said in the comments,
    because "that thread about the tennis analogy" is usually a comment."""
    like = f"%{q.strip()}%"
    posts = [dict(r) for r in con.execute(
        "SELECT id, subreddit, author, title, url, snippet, why, status, prospect, "
        "created_utc, keep FROM posts WHERE title LIKE ? OR snippet LIKE ? "
        "OR why LIKE ? OR subreddit LIKE ? OR COALESCE(author,'') LIKE ? "
        "ORDER BY COALESCE(created_utc, 0) DESC LIMIT ?",
        (like, like, like, like, like, limit))]
    ppl = [dict(r) for r in con.execute(
        "SELECT * FROM people WHERE quote LIKE ? OR author LIKE ? "
        "ORDER BY first_at DESC LIMIT ?", (like, like, limit))]
    return {"q": q, "posts": posts, "people": ppl}


def skip_report(con: sqlite3.Connection) -> dict:
    """What you turn down, and where. This is the material for sharpening the
    rubric: thirty real rejections in your own words beat any amount of guessing
    about what you would want."""
    by_reason = [dict(r) for r in con.execute(
        "SELECT status_reason reason, COUNT(*) n FROM posts "
        "WHERE status = 'skip' GROUP BY reason ORDER BY n DESC")]
    by_sub = [dict(r) for r in con.execute(
        "SELECT subreddit, status_reason reason, COUNT(*) n FROM posts "
        "WHERE status = 'skip' AND status_reason IS NOT NULL "
        "GROUP BY subreddit, reason HAVING n >= 2 ORDER BY n DESC LIMIT 20")]
    notes = [dict(r) for r in con.execute(
        "SELECT subreddit, title, status_reason reason, status_note note "
        "FROM posts WHERE status = 'skip' AND COALESCE(status_note,'') != '' "
        "ORDER BY acted_at DESC LIMIT 30")]
    return {"by_reason": by_reason, "by_sub": by_sub, "notes": notes,
            "labels": SKIP_REASONS}


def merge_posts(con: sqlite3.Connection, rows: list[dict]) -> dict:
    """Absorb what the scraper found, without touching what you decided.

    The scraper machine owns discovery: which posts exist, their scores, the
    model's verdicts. This machine owns judgement of a different kind: what YOU
    did about them. So a merge fills in the first and never overwrites the
    second, and can be run as often as you like.
    """
    fresh = updated = 0
    for r in rows:
        cur = con.execute("SELECT status, acted_at, llm_score FROM posts WHERE id = ?",
                          (r["id"],)).fetchone()
        if cur is None:
            cols = [c for c in r if c not in ("status", "acted_at",
                                              "status_reason", "status_note")]
            con.execute(
                f"INSERT INTO posts ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})", [r[c] for c in cols])
            fresh += 1
            continue
        # Known post: take a verdict we do not have, and the livelier reply count.
        if cur["llm_score"] is None and r.get("llm_score") is not None:
            con.execute("UPDATE posts SET llm_score = ?, keep = ?, why = ?, "
                        "judged_at = ? WHERE id = ?",
                        (r["llm_score"], r.get("keep"), r.get("why"),
                         r.get("judged_at"), r["id"]))
            updated += 1
        con.execute("UPDATE posts SET num_comments = COALESCE(?, num_comments), "
                    "last_seen = COALESCE(?, last_seen), "
                    "pre_score = COALESCE(?, pre_score), "
                    # Whichever side has more of the post keeps it. The collector
                    # owns this column, so carrying it is the same rule as a
                    # verdict, but a shorter body must never win: the two
                    # machines fill it from different runs and only one of them
                    # may have reached the subreddit's feed in time.
                    "post_text = CASE WHEN LENGTH(COALESCE(?, '')) > "
                    "  LENGTH(COALESCE(post_text, '')) THEN ? ELSE post_text END, "
                    "post_text_at = CASE WHEN LENGTH(COALESCE(?, '')) > "
                    "  LENGTH(COALESCE(post_text, '')) THEN ? ELSE post_text_at END, "
                    # Same rule as `upsert`: a side that knows wins, a side that
                    # does not know passes NULL and changes nothing. The two
                    # machines collect through different sources, and only the
                    # runner's OAuth listing can answer this at all, so a laptop
                    # merging its own export up must not blank it.
                    "archived = COALESCE(?, archived), "
                    "locked = COALESCE(?, locked) "
                    "WHERE id = ?",
                    (r.get("num_comments"), r.get("last_seen"),
                     r.get("pre_score"),
                     r.get("post_text"), r.get("post_text"),
                     r.get("post_text"), r.get("post_text_at"),
                     r.get("archived"), r.get("locked"), r["id"]))
    con.commit()
    return {"new": fresh, "verdicts_filled": updated}


def export_posts(con: sqlite3.Connection, since: str | None = None) -> list[dict]:
    """Everything the scraper has found, for handing to the machine you work on."""
    q = "SELECT * FROM posts"
    args: list = []
    if since:
        q += " WHERE COALESCE(last_seen, first_seen) >= ?"
        args.append(since)
    return [dict(r) for r in con.execute(q, args)]


def stats(con: sqlite3.Connection) -> dict:
    row = con.execute(
        "SELECT COUNT(*) total, SUM(llm_score IS NOT NULL) judged, "
        "SUM(COALESCE(keep, 0)) kept, SUM(status = 'posted') posted, "
        "SUM(status = 'skip') skipped, COUNT(DISTINCT subreddit) subs "
        "FROM posts").fetchone()
    return {k: (row[k] or 0) for k in row.keys()}
