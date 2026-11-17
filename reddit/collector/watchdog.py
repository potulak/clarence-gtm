#!/usr/bin/env python3
"""Is anything still collecting Reddit posts? Shout if not.

    python3 reddit/collector/watchdog.py

The failure this exists for is not collection breaking. It is nobody being told.
Collection died on 2026-09-02 and was found on 2026-10-17, because a dead
collector and a quiet queue look identical from the board: the same cards, in
the same order, just not moving. 45 days of recruiting went that way.

So this asks the cloud board one question, how long since anything was
collected, and FAILS when the answer is too long. A failed Actions run mails the
founder by default, which is the whole mechanism: the silence becomes a message.

It runs from GitHub Actions because that is the only part of this setup that has
proven it runs without a machine of his being awake, and it deliberately needs
NO Reddit access, which is why it works where the collector does not.

Stdlib only, and no imports from `store` or `monitor` even though they sit in
this directory. A watchdog that needs the thing it watches is not a watchdog:
this has to still run when the store is missing, the collector is broken and the
Air is face down in a bag.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

# The one number to change. 4 hours: the Air collects AND pushes hourly, so three
# missed hours in a row is already a fault rather than a lull, and a couple of
# hours of slack absorbs a dropped wifi connection or a sync that failed once.
#
# It was 12 hours until 2026-10-18, and 12 was too loose to be a watchdog. That
# morning the Air collected 3 times instead of 24, with a 7.6 hour hole in the
# middle, and this reported "OK: collected 6.8h ago" straight through it. The
# whole job here is turning silence into a message, and a threshold that passes a
# 7.6 hour silence is not doing that job.
#
# What 4 assumes: that the Air stays awake around the clock. That is true and
# enforced now, by `awake.sh on` plus `battery-guard.sh`, and it is deliberate
# that a guard-induced nap on battery DOES trip this. A napping collector is
# exactly the thing he needs to hear about.
#
# If that ever stops being true, this is the number to raise, and `pmset -g sched`
# on the Air is where to read the window from. A nightly sleep of 23:00 to 07:00
# would need about 10 here, or this will mail him every morning, and an alarm that
# cries wolf gets filtered, which is how 45 days went missing in the first place.
# Set CLARENCE_STALE_AFTER_HOURS in the workflow rather than editing this file,
# so the number lives next to the cron that decides how fast it is noticed.
def _stale_after() -> float:
    raw = (os.environ.get("CLARENCE_STALE_AFTER_HOURS") or "").strip()
    if not raw:
        return 4.0
    try:
        hours = float(raw)
    except ValueError:
        hours = 0.0
    if hours <= 0:
        # Never silently fall back to something looser than the default: a typo
        # in a workflow env must not quietly widen the alarm.
        print(f"ignoring CLARENCE_STALE_AFTER_HOURS={raw!r}, using 4.0", flush=True)
        return 4.0
    return hours


STALE_AFTER_HOURS = _stale_after()

# Looser, and only ever a warning. See the note in `check` on why this cannot be
# an error: a queue can legitimately gain no NEW KEEPABLE post for many hours on
# a quiet night, so a tight threshold here would cry wolf and get ignored.
NOTHING_KEPT_WARN_HOURS = 72.0

BOARD = (os.environ.get("CLARENCE_BOARD_URL")
         or "https://clarence-reddit.<your-subdomain>.workers.dev").rstrip("/")
KEY_FILE = (pathlib.Path.home() / "Library" / "Application Support"
            / "clarence-aeo" / "cockpit-key")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def annotate(level: str, title: str, msg: str) -> None:
    """A GitHub annotation, so the Actions page shows it without a mail."""
    if os.environ.get("GITHUB_ACTIONS"):
        one_line = msg.replace("\n", " ")
        say(f"::{level} title={title}::{one_line}")


def summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        pass


def key() -> str | None:
    env = (os.environ.get("COCKPIT_KEY") or "").strip()
    if env:
        return env
    if KEY_FILE.exists():
        k = KEY_FILE.read_text().strip()
        if k:
            return k
    return None


def hours_since(stamp: str | None) -> float | None:
    """Age of an ISO timestamp in hours. Handles both shapes the board emits:
    `2026-10-17T22:57:41.524Z` from the Worker and `2026-10-17T15:57:40-0700`
    from the collector's own queue.json."""
    if not stamp:
        return None
    s = stamp.strip().replace("Z", "+00:00")
    # fromisoformat wants +07:00, the collector writes -0700.
    if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    try:
        when = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return round((dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 3600, 1)


def fetch(k: str) -> dict:
    req = urllib.request.Request(
        f"{BOARD}/api/reddit",
        headers={"Cookie": f"ck={k}",
                 # Cloudflare answers 1010 to urllib's default agent, which
                 # reads as a bot signature.
                 "User-Agent": "clarence-watchdog/1.0 (+reddit cockpit)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def check() -> int:
    k = key()
    if not k:
        # Deliberately NOT a failure. Before the secret exists this workflow has
        # nothing to watch with, and failing here would mail him every run for a
        # setup step, which is exactly the noise this replaced on the collector.
        # It is loud on the Actions page instead, and silent in his inbox.
        say("NOT ARMED: no cockpit key, so nothing was checked.")
        say("")
        say("This watchdog is doing nothing until that is fixed. To arm it, add")
        say("the COCKPIT_KEY repository secret. The value is on the founder's")
        say(f"own Mac at:  {KEY_FILE}")
        say("")
        say("  gh secret set COCKPIT_KEY < ~/Library/Application\\ Support/clarence-aeo/cockpit-key")
        annotate("warning", "Reddit watchdog is not armed",
                 "No COCKPIT_KEY secret, so collection is NOT being watched. "
                 "Add it to turn the silence into a message.")
        summary(["### Reddit watchdog: not armed", "",
                 "No `COCKPIT_KEY` secret, so nothing is being watched.",
                 "", "```bash",
                 "gh secret set COCKPIT_KEY < ~/Library/Application\\ Support/clarence-aeo/cockpit-key",
                 "```"])
        return 0

    say(f"asking {BOARD} how fresh the queue is")
    try:
        data = fetch(k)
    except urllib.error.HTTPError as e:
        why = ("the cockpit key was refused. Check the COCKPIT_KEY secret "
               "against the value on the Mac."
               if e.code == 401 else f"the board answered HTTP {e.code}.")
        say(f"\nFAILED TO CHECK: {why}")
        # Failing to check IS a failure. A watchdog that cannot see is not a
        # reassurance, and treating it as one is how the original 45 days
        # happened.
        annotate("error", "Reddit watchdog could not check",
                 f"{why} Collection freshness is UNKNOWN.")
        summary([f"### Reddit watchdog: could not check", "", why])
        return 1
    except Exception as e:
        say(f"\nFAILED TO CHECK: could not reach the board ({e}).")
        annotate("error", "Reddit watchdog could not reach the board",
                 f"{e}. Collection freshness is UNKNOWN.")
        summary(["### Reddit watchdog: could not reach the board", "", str(e)])
        return 1

    pushed_at = data.get("pushed_at") or (data.get("health") or {}).get("pushed_at")
    pushed_h = hours_since(pushed_at)
    live = data.get("count")
    total = data.get("total")

    # When the newest post in the queue was first filed. A second, independent
    # signal: it catches the case where the sync keeps running and pushing while
    # the collector itself has stopped finding anything, which `pushed_at` alone
    # would report as healthy.
    seen = [t.get("first_seen") for t in (data.get("threads") or [])
            if t.get("first_seen")]
    newest_h = min((h for h in (hours_since(s) for s in seen) if h is not None),
                   default=None)

    say("")
    say(f"  last push from a collector : {pushed_h}h ago ({pushed_at})"
        if pushed_h is not None else
        f"  last push from a collector : UNKNOWN ({pushed_at!r})")
    say(f"  newest post in the queue   : {newest_h}h ago" if newest_h is not None
        else "  newest post in the queue   : none in the queue")
    say(f"  queue                      : {live} live of {total} total")
    say(f"  threshold                  : {STALE_AFTER_HOURS}h")

    if pushed_h is None:
        say("\nSTALE: the board has no record of any push at all.")
        annotate("error", "Reddit collection has never pushed",
                 "The board reports no pushed_at. Nothing has ever reached it.")
        summary(["### Reddit watchdog: no push on record", "",
                 "The board reports no `pushed_at` at all."])
        return 1

    if pushed_h > STALE_AFTER_HOURS:
        say(f"\nSTALE: nothing has been collected for {pushed_h} hours.")
        say("")
        say("What to check, in this order:")
        say("  1. Is the MacBook Air awake, plugged in, and on wifi? That is the")
        say("     collector. It cannot run from a datacenter, because Reddit")
        say("     answers 403 to logged-out reads from one.")
        say("  2. Is its hourly job still loaded?")
        say("     launchctl list | grep clarence.reddit")
        say("  3. Then force a pass and watch it:")
        say("     bash scratch/reddit/hourly.sh && tail -40 scratch/reddit/logs/$(date +%F).log")
        annotate("error", f"Reddit collection stalled for {pushed_h}h",
                 f"Nothing collected for {pushed_h}h (threshold "
                 f"{STALE_AFTER_HOURS}h). Check that the MacBook Air is awake "
                 f"and plugged in, and that its hourly job is loaded.")
        summary([
            f"### Reddit collection has stalled",
            "",
            f"Nothing collected for **{pushed_h} hours** "
            f"(threshold {STALE_AFTER_HOURS}h). Last push: `{pushed_at}`.",
            "",
            "The collector is the MacBook Air, because Reddit answers 403 to "
            "logged-out reads from a datacenter. Check that it is awake, "
            "plugged in, and still running its hourly job:",
            "",
            "```bash",
            "launchctl list | grep clarence.reddit",
            "bash scratch/reddit/hourly.sh",
            "```",
        ])
        return 1

    notes = []
    if newest_h is not None and newest_h > NOTHING_KEPT_WARN_HOURS:
        # A warning, never an error. The queue only holds posts worth answering
        # that he has not acted on, and going a long time without a new one is a
        # real possibility rather than a fault.
        msg = (f"Pushes are arriving ({pushed_h}h ago) but the newest post worth "
               f"answering is {newest_h}h old. Collection is alive; it may be "
               f"finding nothing, or the watchlist may have gone stale.")
        say(f"\nWORTH A LOOK: {msg}")
        annotate("warning", "Collecting, but finding nothing new", msg)
        notes = ["", f"Note: newest keepable post is **{newest_h}h** old."]

    say(f"\nOK: collected {pushed_h}h ago, inside the {STALE_AFTER_HOURS}h threshold.")
    summary([f"### Reddit collection is alive", "",
             f"Last collected **{pushed_h}h** ago, threshold "
             f"{STALE_AFTER_HOURS}h. Queue: **{live}** live of {total}."] + notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
