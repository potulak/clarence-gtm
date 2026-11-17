#!/usr/bin/env python3
"""The day's shortlist: the handful of posts actually worth your morning.

    python3 scratch/reddit/shortlist.py              # today's list
    python3 scratch/reddit/shortlist.py --want 25    # ask for more
    python3 scratch/reddit/shortlist.py --json       # for the board

The collector files roughly two hundred posts a day and the judge scores them.
Neither of those is the thing you need. The thing you need is "which ten to
twenty should I answer today", and that is a different question: it depends on
whether the thread is still alive, whether a reply would be read or buried,
whether you have already spent the morning in that one subreddit, and whether
it is time to go somewhere new.

Three ideas run this file.

ONE: the judge's verdict is the spine but not the whole score. `llm_score` says
how well a post matches the problem Clarence solves. It says nothing about
whether answering is still possible. A perfect post from last Tuesday is worth
less than a good one from this morning, so freshness and room-to-reply are part
of the rank, not a filter applied afterwards.

TWO: a shortlist that is twenty posts from one subreddit is not a shortlist, it
is a single room. r/FriendshipAdvice alone holds 87 of the top-band posts in the
store, so without a cap it would be the entire list every day. The cap is what
makes the day feel like a range of people rather than one feed.

THREE: you cannot break into a community you never post in, and the surfacing
will happily keep you in the three subs that already work. So a slice of every
day is reserved for somewhere you have never posted. It costs a few of the best
slots and it is the only way the ground ever widens.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store  # noqa: E402

# The judge scores in steps of 0.05 and puts most posts at 0.05 or 0.10, so a
# percentile over the raw score is meaningless: more than a third of everything
# sits on one value. These are the bands the scores actually form.
# Raised from 0.55 and 0.30 on 2026-11-06. He reads the drafts as six or seven
# out of ten and wants better, and the supply allows it: of posts caught inside
# five hours, 13.1 a day score 0.5 or more, 11.0 score 0.6, 7.9 score 0.7. Eight
# strong ones a day is a full session, so the bar can afford to move up.
SURE = 0.70        # the judge said yes, and means it
MAYBE = 0.55       # the floor to appear at all. Below this it does not show.

# Five hours, because that is what the evidence actually supports once you ask
# the right question of the right machine.
#
# A thread does keep taking comments for a day or two: mean comments by age ran
# 3.3 at 4 to 6h and 5.4 at 1 to 2 days across 10,274 posts here. That measures
# how long the THREAD lives, which is not the question. The question is whether
# HIS comment gets read, and a comment arriving at hour twenty starts at zero
# votes beneath five that have been collecting them since the OP was still
# watching. Reddit's comment sort has no time term, so a late comment is not
# demoted, it simply never climbs.
#
# His own eighteen posted comments say it plainly: one went out inside five
# hours, the median was about a hundred hours, and none drew a reply. Meanwhile
# the always-on Mac, on the days it actually ran, caught 12 to 16 good posts a
# day inside five hours. The fresh ones were there the whole time and the list
# was handing him week-old threads.
#
# The earlier version of this comment argued for a 24 hour floor on the grounds
# that the pipeline only makes three or four fresh posts a day. That number came
# from a store which had collected nothing for six days, so everything in it was
# old by construction. It was a measurement of a stalled collector, not of the
# pipeline.
ANSWERABLE_H = 12  # the ramp: a post this old scores zero for freshness
# The windows tried in order. A tight default, then wider ones, because a list
# that is empty when collection hiccups is worse than a list with an old post
# in it: an empty board teaches you to stop opening it.
WINDOWS = (5, 12, 24, 48, 168)
# How few picks is still a day worth opening. This is the number that decides
# how fresh the list can be, and it was the real reason the day's list was old.
# Asking for ten forced the window to seven days, because the pipeline does not
# produce ten fresh ones: judged, worth answering, under a day, and not four
# deep in the same subreddit comes to about three or four a day. Three fresh
# threads beat fourteen stale ones, and the whole product elsewhere is built on
# a short list rather than a feed.
# Two, and low on purpose. At the raised bar the supply is about eight strong
# posts a day, so a five hour window holds two or three at any moment. Asking
# for more is what drags the window open, and a list of seven week-old threads
# is worth less than two from this afternoon. He checks the board through the
# day rather than once, so a short fresh list each time is the shape that fits.
FLOOR = 2
PER_SUB = 4        # at most this many from one subreddit in a day
EXPLORE = 0.10     # this share of the day goes to subs you have never posted in


def rank(row: dict, now: float) -> float:
    """One number to order the day by.

    Weighted so the judge dominates (it is the only part that knows what the
    post is ABOUT), but never so much that it can hand you a dead thread.
    """
    llm = row.get("llm_score") or 0.0
    pre = row.get("pre_score") or 0.0
    created = row.get("created_utc") or 0
    age_h = (now - created) / 3600 if created else 999.0
    fresh = max(0.0, 1.0 - age_h / ANSWERABLE_H)
    ncom = row.get("num_comments") or 0
    # Few replies means your answer is the one they read. Past twenty-five it is
    # a footnote however good it is.
    room = 1.0 if ncom <= 8 else 0.6 if ncom <= 25 else 0.3
    # Freshness was a fifth of this and the judge could outvote it with a
    # six-day-old post, which is exactly what the day's list had been serving.
    # Now it is nearly a third, and the window below is a real cutoff rather
    # than a gentle preference.
    return round(0.50 * llm + 0.10 * pre + 0.30 * fresh + 0.10 * room, 4)


def _candidates(con, hours: int) -> list[dict]:
    """Judged, not yet acted on, and still worth reading."""
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM posts WHERE status = 'todo' AND llm_score IS NOT NULL "
        "AND COALESCE(title,'') != '' AND COALESCE(url,'') != '' "
        "AND created_utc > strftime('%s','now') - ? * 3600",
        (hours,))]
    now = time.time()
    for r in rows:
        r["rank"] = rank(r, now)
        r["age_hours"] = round((now - (r["created_utc"] or now)) / 3600, 1)
        r["tier"] = "sure" if (r["llm_score"] or 0) >= SURE else (
            "maybe" if (r["llm_score"] or 0) >= MAYBE else "no")
    return sorted(rows, key=lambda r: -r["rank"])


def posted_subs(con) -> set[str]:
    """Where you have actually shown up. Posting is the only thing that counts:
    reading a sub, or skipping things in it, does not make you present there."""
    return {r["subreddit"].lower() for r in con.execute(
        "SELECT DISTINCT subreddit FROM posts WHERE status = 'posted' "
        "AND subreddit IS NOT NULL")}


def _ladder(con, want: int, floor: int) -> dict:
    """Walk the windows and stop at the first that yields a workable day.

    It counts PICKS, not candidates, and that distinction is the whole point.
    A tight window can hold a hundred and fifty candidates and still produce
    three picks, because the per-subreddit cap and the tiers do most of the
    cutting. Widening on the candidate count therefore declared success on a
    day that had almost nothing to show.
    """
    tried, best = [], None
    for w in WINDOWS:
        out = build(con, want=want, floor=floor, hours=w)
        tried.append((w, len(out["picks"])))
        best = out
        if len(out["picks"]) >= floor:
            break
    best["widened"] = best["window_hours"] != WINDOWS[0]
    best["windows_tried"] = tried
    return best


def build(con, want: int = 20, floor: int = FLOOR,
          hours: int | None = None) -> dict:
    """The day's list, plus enough context to trust it.

    `hours` left unset means walk WINDOWS from tight to wide and stop at the
    first one holding enough to work with. That is deliberately not the same as
    a single generous window: a tight window almost always wins, so the list is
    fresh on a normal day, and the wide ones exist only so a stalled collector
    degrades into old posts clearly labelled rather than into a blank screen.
    The window that won is reported, so the board can say which it is looking at.
    """
    if hours is None:
        return _ladder(con, want, floor)
    pool = _candidates(con, hours)
    tried, widened = [(hours, len(pool))], False
    known = posted_subs(con)
    sure = [r for r in pool if r["tier"] == "sure"]
    maybe = [r for r in pool if r["tier"] == "maybe"]

    picks: list[dict] = []
    per_sub: dict[str, int] = {}

    def take(rows, explore_only=False, limit=None):
        for r in rows:
            if limit is not None and len(picks) >= limit:
                return
            sub = (r["subreddit"] or "").lower()
            if explore_only and sub in known:
                continue
            if per_sub.get(sub, 0) >= PER_SUB:
                continue
            if any(p["id"] == r["id"] for p in picks):
                continue
            per_sub[sub] = per_sub.get(sub, 0) + 1
            r["why_picked"] = ("somewhere new" if sub not in known else
                               "the judge said yes" if r["tier"] == "sure" else
                               "worth a look")
            picks.append(r)

    # The ones the judge is sure about come first, then the day is topped up to
    # the floor, and only then does exploration spend a slot, so a quiet day
    # never trades a real post for a speculative one.
    take(sure, limit=want)
    if len(picks) < floor:
        take(maybe, limit=max(floor, min(want, len(sure) + len(maybe))))
    room_to_explore = max(1, int(want * EXPLORE))
    if len(picks) < want:
        take(sure + maybe, explore_only=True, limit=len(picks) + room_to_explore)

    picks.sort(key=lambda r: -r["rank"])
    return {
        "picks": picks,
        "counts": {
            "sure": len(sure), "maybe": len(maybe),
            "pool": len(pool), "shown": len(picks),
            "new_subs": sum(1 for p in picks
                            if (p["subreddit"] or "").lower() not in known),
        },
        "bar": {"sure_at": SURE, "maybe_at": MAYBE,
                "lowest_shown": picks[-1]["rank"] if picks else None},
        "window_hours": hours,
        # Said out loud, because a list of two-day-old posts looks the same as a
        # fresh one until you check, and that is how a week went by answering
        # threads that had already gone quiet.
        "widened": widened,
        "windows_tried": tried,
        "freshest_hours": min((p.get("age_hours") or 999) for p in picks)
                          if picks else None,
    }


def new_communities(con, limit: int = 8, hours: int = 720) -> list[dict]:
    """Subreddits worth breaking into: they keep producing posts the judge likes
    and you have never posted in them."""
    known = posted_subs(con)
    rows = [dict(r) for r in con.execute(
        "SELECT subreddit, COUNT(*) n, AVG(llm_score) avg_score, "
        "       SUM(llm_score >= ?) good "
        "FROM posts WHERE llm_score IS NOT NULL AND subreddit IS NOT NULL "
        "AND created_utc > strftime('%s','now') - ? * 3600 "
        "GROUP BY subreddit HAVING good > 0 ORDER BY good DESC, avg_score DESC",
        (MAYBE, hours))]
    out = [r for r in rows if (r["subreddit"] or "").lower() not in known]
    for r in out:
        # The average over every post in a sub is mostly the 0.05 noise floor,
        # so it reads as 0.06 for a sub that produces a good post every week.
        # The useful number is how OFTEN a post there is worth answering.
        r["hit_rate"] = round(r["good"] / r["n"], 3) if r["n"] else 0.0
        r["avg_score"] = round(r["avg_score"], 3)
    # Somewhere that lands one in four is better ground than somewhere that
    # lands one in fifty, even if the second has more good posts in absolute
    # terms, because you have to read everything else to find them.
    out.sort(key=lambda r: (-(r["good"] >= 2), -r["hit_rate"], -r["good"]))
    return out[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--want", type=int, default=20)
    ap.add_argument("--floor", type=int, default=10)
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    con = store.connect()
    out = build(con, a.want, a.floor, a.hours)
    out["new_communities"] = new_communities(con)
    if a.json:
        print(json.dumps(out, indent=2, default=str))
        return 0

    c = out["counts"]
    print(f"\n{c['shown']} for today "
          f"({c['sure']} the judge is sure about, {c['maybe']} maybes, "
          f"out of {c['pool']} still open in the last {out['window_hours']//24} days)\n")
    for i, p in enumerate(out["picks"], 1):
        mark = "**" if p["tier"] == "sure" else "  "
        print(f"{mark}{i:>2}. [{p['rank']:.2f}] r/{p['subreddit']}  "
              f"{p['age_hours']:.0f}h  {p['num_comments'] or 0} replies  "
              f"({p['why_picked']})")
        print(f"      {p['title'][:88]}")
        if p.get("why"):
            print(f"      {p['why'][:88]}")
        print(f"      {p['url']}")
    nc = out["new_communities"]
    if nc:
        print(f"\nnever posted in these, and they keep producing:")
        for r in nc:
            print(f"   r/{r['subreddit']:<24} {r['good']:>3} good of {r['n']:>4}  "
                  f"({r['hit_rate']*100:.0f}% hit rate)")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
