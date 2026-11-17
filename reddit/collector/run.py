#!/usr/bin/env python3
"""One scheduled pass of the Reddit loop, on a machine nobody owns.

    python3 reddit/collector/run.py --preflight   # check access, spend nothing
    python3 reddit/collector/run.py               # collect, judge, push

The order matters and is the same one the laptop uses:

  1. load the store that travelled in from the store branch
  2. PULL decisions from the cloud first, so a post triaged on the phone is not
     republished as though it were still waiting
  3. collect and judge
  4. PUSH the rebuilt queue up for the phone
  5. write the store back out for the next run

Why authenticated reads are not optional here, measured 2026-10-17 from an
Actions runner: logged-out HTML is 403 Blocked on both www and old.reddit, the
JSON endpoints are 403, and the Atom feeds answer once and then 429 because the
budget is per IP and a shared runner address has usually spent it. There is no
unauthenticated path from a datacenter, so this refuses to run without
credentials rather than reporting a clean run that collected nothing. That
failure already happened once on a residential IP (a whole day collected as
zeroes while a login page was parsed as an empty listing) and it is the single
most expensive way for this to break: a dead collector looks exactly like a
quiet queue.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sqlite3
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import store            # noqa: E402
import monitor          # noqa: E402
import cloud            # noqa: E402
import storefile        # noqa: E402

# What a single run may judge, per lane. Lower than the laptop's 60/30 because
# this runs on a schedule and the laptop did not: a cap that is fine once an
# afternoon is a different thing eight times a day. Tunable without a code
# change, because the right number depends on a cadence set in the workflow.
RUN_BUDGET = {
    "conversations": int(os.environ.get("CLARENCE_BUDGET_CONVERSATIONS", "40")),
    "launch": int(os.environ.get("CLARENCE_BUDGET_LAUNCH", "15")),
}

# A hard ceiling on judging, over a rolling 30 days, read back out of the store's
# own spend table. The Perplexity key in docs/URGENT.md sat dead for two months
# because nothing checked; this is the same lesson applied to the opposite
# failure, which is a schedule quietly spending more than anyone intended.
SPEND_CEILING_USD = float(os.environ.get("CLARENCE_SPEND_CEILING_USD", "20"))


def say(msg: str = "") -> None:
    print(msg, flush=True)


def summary(lines: list[str]) -> None:
    """Also write to the run summary, so the founder can read the outcome from
    his phone without opening a log."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        pass


def spend_30d(con: sqlite3.Connection) -> float:
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(usd), 0) FROM spend "
            "WHERE at >= datetime('now', '-30 days')").fetchone()
        return float(row[0] or 0.0)
    except sqlite3.Error:
        return 0.0


def preflight() -> int:
    """Everything that can be wrong, said plainly, before a cent is spent."""
    problems = []
    say("Reddit API access")
    ok, why = monitor.oauth_ready()
    say(f"  {'yes' if ok else 'NO'}: {why}")
    if not ok:
        problems.append("Reddit credentials")
    else:
        # Prove a real read works. A token alone does not: the whole open
        # question about this design is whether Reddit serves authenticated
        # reads to a datacenter IP, and one listing answers it.
        got = monitor._oauth_multireddit_new(["lostafriend", "FriendshipAdvice"], 1)
        say(f"  read a listing: {len(got)} posts")
        if not got:
            say("  NO: authenticated but the listing came back empty. If this "
                "persists, Reddit is refusing API reads from this address and "
                "the collector cannot live here.")
            problems.append("Reddit API reads from this runner")

    say("")
    say("Anthropic key (judging)")
    monitor.load_env()
    if os.environ.get("ANTHROPIC_API_KEY"):
        say("  yes")
    else:
        say("  NO: no ANTHROPIC_API_KEY. Collection would still work and the "
            "free ranking still stands, but nothing would be judged.")
        problems.append("Anthropic key")

    say("")
    say(f"Cloud board ({cloud.BOARD})")
    try:
        res = cloud.call("/api/reddit/actions?since=")
        say(f"  yes: {len(res.get('actions') or [])} decisions on record")
    except cloud.CloudError as e:
        say(f"  NO: {e}")
        problems.append("cloud board")

    say("")
    if problems:
        say("NOT READY. Missing: " + ", ".join(problems))
        summary([f"### Reddit collector preflight: not ready",
                 "", "Missing: " + ", ".join(problems)])
        return 2
    say("Ready. Everything the scheduled run needs is reachable.")
    summary(["### Reddit collector preflight: ready"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preflight", action="store_true",
                    help="check access and spend nothing")
    ap.add_argument("--store", default=os.environ.get("CLARENCE_STORE_SQL", ""),
                    help="the dump that travelled in, and is written back out")
    ap.add_argument("--no-cloud", action="store_true",
                    help="skip the pull and the push (for a local dry run)")
    ap.add_argument("--no-judge", action="store_true",
                    help="collect only, spend nothing on the model")
    a = ap.parse_args()

    monitor.load_env()
    monitor.set_source("oauth")

    if a.preflight:
        return preflight()

    # Refuse rather than collect nothing. See the note at the top of this file.
    ok, why = monitor.oauth_ready()
    if not ok:
        say(f"STOPPING: {why}")
        summary(["### Reddit collector: stopped before collecting", "",
                 f"`{why}`", "",
                 "No unauthenticated path to Reddit exists from a datacenter "
                 "address, so this run would have collected nothing while "
                 "reporting success. See `reddit/collector/README.md`."])
        return 2

    t0 = time.time()
    sql = pathlib.Path(a.store) if a.store else None

    # 1. the store that travelled in
    if sql:
        got = storefile.load(sql, store.DB)
        if got["found"]:
            say(f"store in: {got['posts']} posts, {got['judged']} judged, "
                f"{got['posted']} posted, {got['skipped']} skipped, "
                f"{got['actions']} actions, {got['saved_people']} saved people")
        else:
            say(f"store in: nothing at {sql}. First run, starting empty. "
                f"Everything collected will be judged as new.")
    else:
        say(f"store: using {store.DB} in place, nothing carried in or out")

    con = store.connect()

    # 2. decisions first, so nothing you already triaged comes back
    pulled = {}
    if not a.no_cloud:
        try:
            pulled = cloud.pull(con)
            say(f"pulled {pulled['seen']} decisions, applied {pulled['applied']}"
                f" ({pulled['already_current_or_unknown']} already current here "
                f"or about a post this runner has never seen)")
        except cloud.CloudError as e:
            # Not fatal. Collecting is still worth doing, and apply_action is
            # idempotent so the next run picks these up. Failing the whole run
            # over a sync would turn a bad minute into a gap in collection.
            say(f"  ! could not pull decisions ({e}). Collecting anyway.")

    # 3. the money step, with a ceiling
    budget = dict(RUN_BUDGET)
    if a.no_judge:
        budget = {k: 0 for k in budget}
        say("judging off by request, collecting only")
    else:
        spent = spend_30d(con)
        if spent >= SPEND_CEILING_USD:
            budget = {k: 0 for k in budget}
            say(f"JUDGING PAUSED: ${spent:.2f} spent in the last 30 days, "
                f"ceiling is ${SPEND_CEILING_USD:.2f}. Collecting anyway, so "
                f"nothing is missed, and the free ranking still stands. Raise "
                f"CLARENCE_SPEND_CEILING_USD to resume.")
        else:
            say(f"spend so far over 30 days: ${spent:.2f} of "
                f"${SPEND_CEILING_USD:.2f}")
    con.close()

    # 4. collect and judge. monitor.main owns this and is the same code the
    #    laptop runs, so there is one collector and not two to keep in step.
    rc = monitor.main(top_pct=0.10, source="oauth", budget=budget)

    con = store.connect()
    st = store.stats(con)

    # 5. up to the phone
    pushed = {}
    if not a.no_cloud:
        try:
            pushed = cloud.push(con)
            say(f"pushed {pushed['threads']} threads to the board")
        except cloud.CloudError as e:
            say(f"  ! could not push the queue ({e}). The store still has "
                f"everything; the next run carries it up.")

    spent_now = spend_30d(con)
    con.close()

    # 6. and back out for the next run
    if sql:
        info = storefile.dump(store.DB, sql)
        say(f"store out: {info['bytes'] / 1024:.0f} KB, {info['posts']} posts, "
            f"{info['full_text_rows']} with full text, decisions intact "
            f"({info['posted']} posted, {info['skipped']} skipped, "
            f"{info['actions']} actions, {info['saved_people']} saved people)")

    mins = (time.time() - t0) / 60
    say(f"\ndone in {mins:.1f} min")
    summary([
        "### Reddit collector",
        "",
        f"- store: **{st['total']}** posts across **{st['subs']}** subreddits, "
        f"**{st['judged']}** judged, **{st['kept']}** worth answering",
        f"- your record: **{st['posted']}** posted, **{st['skipped']}** skipped",
        f"- queue pushed to the phone: **{pushed.get('threads', 0)}** threads",
        f"- decisions pulled down: **{pulled.get('applied', 0)}** applied of "
        f"**{pulled.get('seen', 0)}** on record",
        f"- judging spend, rolling 30 days: **${spent_now:.2f}** of "
        f"${SPEND_CEILING_USD:.2f}",
        f"- took {mins:.1f} min",
    ])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
