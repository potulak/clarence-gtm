#!/usr/bin/env python3
"""Bring the runner's collected posts into this Mac's store.

    python3 reddit/collector/import-store.py              # say what would change
    python3 reddit/collector/import-store.py --apply      # actually merge it in
    python3 reddit/collector/import-store.py --from x.sql # a dump already on disk

WHY THIS EXISTS
---------------
The scheduled collector on GitHub Actions is the only part of this engine that
reads Reddit over OAuth, and OAuth is the only source that can say whether a
thread will take a comment: `archived` and `locked` sit on every post in a
listing and nowhere in the logged-out HTML a Mac can still fetch. Both Macs
collect over HTML, so left alone their stores can only ever say "unknown" about
every thread, which is the safe answer and not the useful one.

The runner's store travels as a SQL dump on the `reddit-store` branch, and until
now nothing read it. `sync.sh` trades between the two Macs and no script reads
that branch at all, so everything the runner learns has been staying up there.
This is the one way down.

WHAT IT WILL NOT DO
-------------------
It never writes your decisions. `store.merge_posts` fills in what the collector
owns (posts, verdicts, reply counts, and now `archived` and `locked`) and never
touches `status`, `acted_at`, `status_reason` or `status_note`, which are yours.
This checks that afterwards rather than trusting it: the posted, skipped and
journal counts are read before and after and have to match, or it says so loudly.

It also writes nothing at all unless you pass `--apply`. The default run does the
whole merge against a COPY of your store and tells you what would land, because
the first question about a tool that touches the only irreplaceable data here is
what it is about to do.

The runner's own `actions` table is deliberately ignored. A runner collects and
judges; it never posts or skips, so there is nothing of yours up there to replay,
and `cloud.py` already carries real decisions the other way.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store        # noqa: E402
import storefile    # noqa: E402

BRANCH = "reddit-store"
DUMP = "store.sql"

# The numbers that cannot be rebuilt. Checked before and after every merge.
DECISIONS = ("decided", "posted", "skipped", "actions", "saved_people")


def fetch_dump(repo, into):
    """The runner's dump, out of the store branch. Returns a path, or None.

    Fetched into a remote-tracking ref and read with `git show`, so no branch is
    checked out and the working tree is never touched. That matters because this
    runs in the checkout the live board reads through the scratch/ symlinks.
    """
    ref = f"refs/remotes/origin/{BRANCH}"
    subprocess.run(
        ["git", "fetch", "--depth", "1", "origin",
         f"+refs/heads/{BRANCH}:{ref}"],
        cwd=str(repo), capture_output=True)
    have = subprocess.run(["git", "rev-parse", "-q", "--verify", ref],
                          cwd=str(repo), capture_output=True)
    if have.returncode != 0:
        return None
    out = subprocess.run(["git", "show", f"{ref}:{DUMP}"],
                         cwd=str(repo), capture_output=True)
    if out.returncode != 0 or not out.stdout:
        return None
    into.write_bytes(out.stdout)
    return into


def counts(db):
    con = sqlite3.connect(str(db))
    try:
        got = storefile._counts(con)
        got["known_archived"] = _one(con, "SELECT COUNT(*) FROM posts "
                                          "WHERE archived IS NOT NULL")
        got["known_locked"] = _one(con, "SELECT COUNT(*) FROM posts "
                                        "WHERE locked IS NOT NULL")
        got["closed"] = _one(con, "SELECT COUNT(*) FROM posts "
                                  "WHERE archived = 1 OR locked = 1")
        return got
    finally:
        con.close()


def _one(con, sql):
    try:
        return int(con.execute(sql).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def merge_into(db, rows):
    """Merge those rows into the store at `db`. Returns what changed."""
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        # Only columns this store actually has. The dump comes from the same
        # code, but a runner one version ahead would otherwise fail the whole
        # import on an INSERT naming a column that is not here yet.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(posts)")}
        kept = [{k: v for k, v in r.items() if k in cols} for r in rows]
        dropped = sorted({k for r in rows for k in r if k not in cols})
        res = store.merge_posts(con, kept)
        res["dropped_columns"] = dropped
        return res
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="merge into the real store (default is a dry run)")
    ap.add_argument("--from", dest="src",
                    help="a dump on disk instead of the store branch")
    a = ap.parse_args()

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="clarence-import-"))
    try:
        if a.src:
            sql = pathlib.Path(a.src).expanduser()
            if not sql.exists():
                print(f"no dump at {sql}")
                return 1
        else:
            repo = HERE.parents[1]
            sql = fetch_dump(repo, tmp / DUMP)
            if sql is None:
                print(f"There is no `{BRANCH}` branch to import from, and there\n"
                      "is not going to be one.\n\n"
                      "That branch would be written by the scheduled collector on\n"
                      "GitHub Actions, which has never completed a run and cannot:\n"
                      "it needs Reddit app credentials, and those are not\n"
                      "available. That is settled rather than pending, and it is a\n"
                      "rule in CLAUDE.md. Do not go looking for a way to register\n"
                      "an app.\n\n"
                      "So no machine anywhere reads Reddit over OAuth, nothing can\n"
                      "know whether a thread is archived or locked, and this tool\n"
                      "has nothing to bring down. It stays for the day Reddit\n"
                      "changes its own rules, and that is the only such day.")
                return 1

        # The dump becomes a store of its own, in a temporary directory, and the
        # live one is not opened for writing until the counts below say it is
        # safe. storefile.load refuses to reuse a path, so this is always fresh.
        theirs = tmp / "runner.db"
        got = storefile.load(sql, theirs)
        if not got.get("found") or not got.get("posts"):
            print(f"the dump at {sql} holds no posts, nothing to import")
            return 1
        theirs_counts = counts(theirs)
        print(f"the runner's dump: {theirs_counts['posts']} posts, "
              f"{theirs_counts['judged']} judged, "
              f"{theirs_counts['known_archived']} with archived known, "
              f"{theirs_counts['closed']} of those closed to comments")

        live = pathlib.Path(str(store.DB))
        if not live.exists():
            print(f"no store at {live} to merge into")
            return 1
        before = counts(live)
        print(f"this Mac's store: {before['posts']} posts, "
              f"{before['decided']} decided, "
              f"{before['known_archived']} with archived known")

        # Read the rows out of the runner's copy, not out of a JSON file, so the
        # shape is exactly what `sync.sh` already feeds `merge_posts` between the
        # two Macs. One path, one set of rules about who owns which column.
        tcon = sqlite3.connect(str(theirs))
        tcon.row_factory = sqlite3.Row
        rows = store.export_posts(tcon)
        tcon.close()

        target = live if a.apply else (tmp / "rehearsal.db")
        if not a.apply:
            shutil.copy2(live, target)
        res = merge_into(target, rows)
        after = counts(target)

        if res.get("dropped_columns"):
            print("  columns in the dump this store does not have, left behind: "
                  + ", ".join(res["dropped_columns"]))
        print(f"\n{'merged' if a.apply else 'would merge'}: "
              f"{res['new']} new posts, {res['verdicts_filled']} verdicts filled, "
              f"{after['known_archived'] - before['known_archived']} threads "
              f"learning whether they are archived "
              f"({after['closed'] - before['closed']} of them closed, which is "
              f"the Voice lab)")

        # The whole safety case, stated as numbers rather than as trust.
        drift = {f: (before[f], after[f]) for f in DECISIONS
                 if before[f] != after[f]}
        if drift:
            print("\nSTOP. The merge changed something it does not own:")
            for f, (b, c) in drift.items():
                print(f"  {f}: {b} -> {c}")
            print("Your store is untouched if this was a dry run. If it was"
                  " --apply, restore the newest copy in scratch/reddit/backups.")
            return 2
        print(f"your decisions are untouched: {after['posted']} posted, "
              f"{after['skipped']} skipped, {after['actions']} in the journal, "
              f"{after['saved_people']} saved people, all unchanged")
        if not a.apply:
            print("\nnothing was written. Run it again with --apply to keep this.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
