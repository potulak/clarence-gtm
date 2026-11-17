#!/usr/bin/env python3
"""The store, as a file a runner can carry between runs.

An Actions run starts with an empty disk. Without somewhere to put the store it
cannot dedupe, so every run would re-judge the same top slice and pay for it
again; and it could not compute the subreddit priors, so the ranking would lose
the half of it that is learned from what the founder actually posted to.

So the store travels, as a text SQL dump on a branch of its own. Why that and
not the alternatives:

  an Actions cache    evicted after 7 days idle, and under repo size pressure,
                      with no warning. A miss is an empty store, which silently
                      re-judges thousands of posts and costs real money. Caches
                      are a speed trick, not storage.
  an Actions artifact durable only for its retention window, immutable, and you
                      have to go hunting through the API for the last successful
                      run to find it. Gone if the run is deleted.
  D1, over the Worker  the right eventual home, the wrong first move. D1 holds a
                      rebuildable `queue` projection and the append-only
                      `actions` journal. Making it the store means porting 3,900
                      posts up there and reimplementing `unjudged`, `sub_priors`
                      and `build_queue` as HTTP endpoints, which is store.py
                      rewritten over the network, with the irreplaceable data
                      behind an API written in an afternoon.
  git                  durable with no expiry, already proven in this repo (the
                      AEO workflow has committed its ledger every month for
                      months), needs no new credential, and every run is a diff
                      you can read and revert. For data that encodes the
                      founder's decisions, being able to see and undo every
                      change beats being elegant.

A TEXT dump rather than the .db file, because a SQLite file is an opaque blob:
git stores a whole new copy of it on every commit and shows "binary files
differ". A dump is line oriented, so git deltas it (measured: two runs of a
1.5MB dump pack into 308KB), the diff is readable, and a bad write is visible
rather than inferred.

Nothing here is the record of a decision. The records are the Mac's reddit.db,
the cloud's append-only `actions` table, and scratch/reddit/backups. This file
is derived from them: if the branch were deleted outright the worst case is one
expensive re-judge, not a lost decision.
"""
from __future__ import annotations

import pathlib
import shutil
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store            # noqa: E402

# Rows whose text is still needed: anything worth answering, anything the
# founder has touched, and anything not yet judged. Everything else is a post a
# model already read and rejected, which we keep forever as a skeleton because
# it is what stops us paying to judge it twice and what teaches the priors.
KEEP_TEXT = "keep = 1 OR status != 'todo' OR llm_score IS NULL"

# Dropped from the travelling copy, not from the Mac. `events` is the trail of
# every step of a review, which is being gathered as a training set and belongs
# where the reviewing happens.
DROP_TABLES = ("events",)


def load(sql_path: pathlib.Path, db_path: pathlib.Path) -> dict:
    """Rebuild a store from a dump. Returns what arrived, for the log."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    if not sql_path.exists():
        return {"found": False, "posts": 0, "decided": 0}
    con = sqlite3.connect(db_path)
    con.executescript(sql_path.read_text())
    con.commit()
    out = _counts(con)
    con.close()
    out["found"] = True
    return out


def _counts(con: sqlite3.Connection) -> dict:
    def one(sql: str) -> int:
        try:
            return int(con.execute(sql).fetchone()[0] or 0)
        except sqlite3.Error:
            return 0
    return {
        "posts": one("SELECT COUNT(*) FROM posts"),
        "judged": one("SELECT COUNT(*) FROM posts WHERE llm_score IS NOT NULL"),
        # The irreplaceable numbers. Every check below is against these.
        "decided": one("SELECT COUNT(*) FROM posts WHERE status != 'todo'"),
        "posted": one("SELECT COUNT(*) FROM posts WHERE status = 'posted'"),
        "skipped": one("SELECT COUNT(*) FROM posts WHERE status = 'skip'"),
        "actions": one("SELECT COUNT(*) FROM actions"),
        "saved_people": one("SELECT COUNT(*) FROM people WHERE saved = 1"),
    }


def dump(db_path: pathlib.Path, sql_path: pathlib.Path,
         work_dir: pathlib.Path | None = None) -> dict:
    """Write the travelling copy, pruned, after proving nothing was lost.

    Prunes a COPY and never the live file, so a bug here cannot reach the store
    the run just spent money filling. Then asserts the decision counts came
    through untouched and raises if they did not, because a dump that quietly
    dropped a decision is worse than a run that failed.
    """
    work_dir = work_dir or sql_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = work_dir / "prune.db"
    if tmp.exists():
        tmp.unlink()
    shutil.copy2(db_path, tmp)

    live = sqlite3.connect(db_path)
    before = _counts(live)
    live.close()

    con = sqlite3.connect(tmp)
    for t in DROP_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {t}")
    # Text only. Never status, acted_at, status_reason or status_note, and never
    # a whole row: those are the founder's, and a skeleton row still carries them.
    con.execute(
        f"UPDATE posts SET snippet = NULL, why = NULL, title = NULL, url = NULL, "
        f"matched_phrase = NULL, found_via = NULL, post_rule = NULL "
        f"WHERE NOT ({KEEP_TEXT})")
    con.commit()
    con.execute("VACUUM")

    after = _counts(con)
    for field in ("posts", "decided", "posted", "skipped", "actions",
                  "saved_people"):
        if after[field] != before[field]:
            con.close()
            raise RuntimeError(
                f"refusing to write the store dump: pruning changed {field} "
                f"from {before[field]} to {after[field]}. Nothing was written "
                f"and the live store is untouched.")

    text = "\n".join(con.iterdump())
    con.close()
    tmp.unlink(missing_ok=True)
    sql_path.parent.mkdir(parents=True, exist_ok=True)
    sql_path.write_text(text + "\n")
    return {**after, "bytes": len(text),
            "full_text_rows": _full_rows(db_path)}


def _full_rows(db_path: pathlib.Path) -> int:
    con = sqlite3.connect(db_path)
    try:
        return int(con.execute(
            f"SELECT COUNT(*) FROM posts WHERE {KEEP_TEXT}").fetchone()[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def main() -> int:
    """Hand check: python3 reddit/collector/storefile.py <out.sql>"""
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "store.sql")
    info = dump(store.DB, out)
    print(f"wrote {out} ({info['bytes'] / 1024:.0f} KB)")
    print(f"  {info['posts']} posts, {info['full_text_rows']} with full text")
    print(f"  decisions carried: {info['posted']} posted, {info['skipped']} "
          f"skipped, {info['actions']} in the journal, "
          f"{info['saved_people']} saved people")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
