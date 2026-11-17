#!/usr/bin/env python3
"""Trade data between this Mac's store and the cloud board.

    python3 scratch/cloud/sync-cloud.py            # pull decisions, then push queue
    python3 scratch/cloud/sync-cloud.py --pull     # decisions only
    python3 scratch/cloud/sync-cloud.py --push     # queue only

Direction of truth, unchanged from the two-laptop design:

    this Mac  owns posts, scores, verdicts   ->  pushed up, replacing `queue`
    the cloud owns your decisions            ->  pulled down, replayed into `posts`

Neither side writes the other's columns, so there is nothing to merge and no
conflict to resolve. `queue` in the cloud is a projection that can be dropped and
rebuilt at any time. `actions` up there is the only thing that cannot.

PULL RUNS FIRST, always. Pushing first would republish a post you triaged on the
phone as though it were still waiting, and you would see it again.

Talks HTTPS to the Worker with the cockpit key, NOT wrangler. That is what lets
any Mac be the collector: wrangler authenticates per machine with its own OAuth
login, so using it here would mean a second interactive login, or a Cloudflare
API token sitting in a file on every collector. The cockpit key is already needed
to read the board, so this adds no new secret anywhere.
"""
# The collector may be the Air, which runs macOS system Python 3.9, while this
# Mac runs 3.14. `dict | None` in a signature is evaluated at import time before
# 3.10, so without this the whole script dies on import over there.
from __future__ import annotations

import argparse
import json
import pathlib
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

def _run_dir() -> pathlib.Path:
    """Where this script's state and the deploy tree sit, which is not where it sits.

    This file is version controlled now, so that the Worker it pushes to can be
    rebuilt from source if the laptop holding it dies. Its state is not, and must
    not be: the watermarks record how far the founder's own decisions have been
    replayed, and `public/` is a deploy tree. So code and state part company
    here, the same way `store._data_dir` splits the collector from its database.

    The answer is the directory this script has always run in, `scratch/cloud`,
    worked out from this file's position rather than from a variable someone has
    to remember to set. That matters more here than it looks: a watermark
    resolving to a fresh path reads as "no decisions pulled yet", and the pull
    would replay the entire cloud journal from the beginning.

    `__file__` is resolved first, so this answers the same whether the script is
    run through the symlink in `scratch/cloud` or by its real path in the repo.
    """
    here = pathlib.Path(__file__).resolve().parent
    if here.name == "cloud" and here.parent.name == "reddit":
        return here.parents[1] / "scratch" / "cloud"   # reddit/cloud -> <repo>/scratch/cloud
    return here                                        # still running from the old location


HERE = _run_dir()
REDDIT = HERE.parent / "reddit"
WATERMARK = HERE / ".pull-watermark"
# Decisions get their own mark. See the note in pull().
DWATERMARK = HERE / ".pull-watermark-decisions"
KEY_FILE = (pathlib.Path.home() / "Library" / "Application Support"
            / "clarence-aeo" / "cockpit-key")
BOARD = "https://clarence-reddit.<your-subdomain>.workers.dev"

sys.path.insert(0, str(REDDIT))
import store            # noqa: E402
import monitor          # noqa: E402


def key() -> str:
    if not KEY_FILE.exists():
        sys.exit(f"No cockpit key at {KEY_FILE}. Copy it from the Mac that has one.")
    k = KEY_FILE.read_text().strip()
    if not k:
        sys.exit(f"The cockpit key at {KEY_FILE} is empty.")
    return k


def call(path: str, payload: dict | None = None, timeout: int = 120) -> dict:
    """One request to the board. Any failure exits without touching the store."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BOARD}{path}", data=data,
        headers={"Content-Type": "application/json", "Cookie": f"ck={key()}",
                 # Cloudflare answers 1010 to urllib's default agent, which reads
                 # as a bot signature. Naming ourselves honestly gets us through
                 # and makes the traffic identifiable in the logs.
                 "User-Agent": "clarence-sync/1.0 (+reddit cockpit)"},
        method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        sys.exit(f"board said HTTP {e.code} for {path}: {body}")
    except Exception as e:
        sys.exit(f"could not reach the board ({e}). Nothing changed locally.")


# The two voices the board offers: "new here" and "the builder".
VOICES = ("curious-newcomer", "me-founder")


def asks_ready() -> bool:
    """Does this machine's store know about asking people to try the app?

    The `asks` table and its four functions arrived with the replies work and
    may not be on every machine yet. Everything else on the board stands without
    them, so a store that has not caught up loses the "already asked" marks and
    keeps its deck, rather than the whole day's list failing to travel because
    one optional column is missing.
    """
    return all(hasattr(store, f) for f in
               ("asked_authors", "record_ask", "set_ask_outcome", "ask_stats"))



def persona_system(author_id: str = "me-founder") -> str | None:
    """The system prompt this Mac would use, resolved here so the cloud holds no
    persona logic of its own and there is still one place to edit a persona."""
    try:
        import enrich
        return enrich._author_system(enrich.load_author(author_id))
    except Exception as e:
        print(f"  ! persona not carried up ({e}), the phone will use a plain one")
        return None


def personas() -> dict:
    """Every voice the board can write as, resolved here for the same reason.

    The phone offers a choice between them, and sending only the default meant
    picking "new here" on the phone quietly wrote as the founder anyway.
    """
    out = {}
    for v in VOICES:
        got = persona_system(v)
        if got:
            out[v] = got
    return out


def survey_url() -> str | None:
    try:
        import enrich
        return enrich.SURVEY_URL
    except Exception:
        return None


def pull() -> int:
    """Bring phone decisions down and replay them into the real store.

    Two kinds travel, because they replay differently.

    A STATUS is one word per post and goes through `store.apply_action`, which
    knows the last-write-wins rule. That is `/api/reddit/actions`.

    Everything else is not a status: asking a person to try the app, what came
    back, clearing a reply, saving someone. Each lands somewhere different in the
    store, so each carries its own payload and gets its own replay. That is
    `/api/reddit/decisions`, and before it existed the phone's "save this person"
    was written as `status = 'prospect'`, which either overwrote a real decision
    or was dropped. Checked on 2026-10-17: eight such saves, all seven people
    lost.

    Drafts come down the same call. A draft is neither: it is work in progress,
    and it has to reach the laptop or dictating an angle on the train and
    finishing it at the desk does not work.
    """
    since = WATERMARK.read_text().strip() if WATERMARK.exists() else ""
    res = call("/api/reddit/actions?since=" + urllib.parse.quote(since))
    rows = res.get("actions") or []

    con = store.connect()
    applied = 0
    for r in rows:
        if store.apply_action(con, r["post_id"], r["status"],
                              r.get("reason") or None, r.get("note") or None,
                              r["at"]):
            applied += 1
    con.close()

    if rows:
        skipped = len(rows) - applied
        # apply_action declines for two different reasons, and conflating them
        # would hide a real problem: a post this machine never collected looks
        # exactly like a decision that is already current here.
        tail = (f", {skipped} not applied (unknown post here, or already current)"
                if skipped else "")
        print(f"  pulled {len(rows)} decisions, applied {applied}{tail}")
    else:
        print("  no new status decisions from the phone")

    # Two marks, not one. A decision this machine cannot record yet parks the
    # decision stream where it is, and a status must never be made to wait
    # behind it: statuses are the irreplaceable half.
    high = max((r["at"] for r in rows), default="")
    if high:
        WATERMARK.write_text(high + "\n")
    dsince = DWATERMARK.read_text().strip() if DWATERMARK.exists() else ""
    dmark = pull_decisions(dsince)
    if dmark and dmark != dsince:
        DWATERMARK.write_text(dmark + "\n")
    return applied


def pull_decisions(since: str) -> str:
    """The decisions that are not statuses, plus any draft in progress.

    Replayed STRICTLY IN ORDER, and the watermark stops at the last one that
    actually landed. If a row cannot be applied here yet, this stops there
    rather than skipping it: skipping would move the mark past a decision that
    was never recorded, and it would be gone for good.

    The one row that can fail that way today is an ask. `record_ask` and its
    `asks` table arrived with the replies work and are not on every machine
    yet, so a store that has not caught up parks those and keeps everything
    before them. Statuses are pulled on a separate mark and never wait for this.

    Returns the watermark to keep. Never raises: a board that has not been
    deployed with this endpoint yet must not take the hourly collector down.
    """
    try:
        res = call("/api/reddit/decisions?since=" + urllib.parse.quote(since))
    except SystemExit:
        # `call` exits on an HTTP error, and a 404 here means an older Worker.
        print("  (this board has no /decisions yet, deploy the Worker)")
        return since
    rows = res.get("decisions") or []
    drafts = res.get("drafts") or []
    if not rows and not drafts:
        return since

    con = store.connect()
    counts: dict = {}
    mark = since
    held = None
    for r in rows:
        kind = r.get("kind") or ""
        who = r.get("subject") or ""
        try:
            load = json.loads(r.get("payload") or "{}")
        except Exception:
            load = {}
        if kind in ("ask_sent", "ask_outcome") and not asks_ready():
            held = kind
            break
        try:
            if kind == "prospect":
                store.set_prospect(con, who, bool(load.get("on")),
                                   load.get("note") or None)
            elif kind == "ask_sent":
                # Once per person. The phone cannot see this machine's asks
                # table, so a replay of the same tap must not add a second row
                # and make the ask count wrong.
                if who.lower() not in store.asked_authors(con):
                    store.record_ask(con, who, post_id=load.get("post_id"),
                                     reply_id=load.get("reply_id"),
                                     channel=load.get("channel") or "thread",
                                     text=load.get("text"))
            elif kind == "ask_outcome":
                store.set_ask_outcome(con, who, load.get("outcome") or "sent")
            elif kind == "reply_ack":
                store.ack_reply(con, who, bool(load.get("on", True)))
            elif kind == "event":
                store.log_event(con, who or None,
                                (load.get("kind") or "note")[:40],
                                load.get("detail")
                                if isinstance(load.get("detail"), dict) else None,
                                origin="phone")
            elif kind == "voice":
                # One step of writing a reply, with its words. Replaying the
                # same row twice is harmless: `capture` drops text identical to
                # the last thing kept for that thread, and a `final` is forced
                # through on purpose, so the only cost of a double pull is a
                # duplicate final, which the episode reading already ignores
                # because it reads the LAST one.
                store.capture(con, who or None, str(load.get("step") or "edit"),
                              load.get("text"),
                              author=load.get("author"),
                              instruction=load.get("instruction"),
                              force=load.get("step") == "final")
            else:
                mark = r.get("at") or mark
                continue
            counts[kind] = counts.get(kind, 0) + 1
        except Exception as e:
            # Something unexpected about THIS row. Stop here for the same reason
            # as above, and say which one, rather than quietly walking past it.
            print(f"  ! could not replay {kind} for {who}: {e}")
            held = kind
            break
        mark = r.get("at") or mark
    con.close()

    if counts:
        print("  pulled " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    if held:
        print(f"  stopped at a '{held}' this store cannot record yet. It and "
              f"everything after it stay in the cloud and replay next time.")
    # Drafts are keyed by post and merged field by field, so replaying one
    # changes nothing. They can travel even when a decision above is parked.
    if drafts:
        print(f"  pulled {merge_drafts(drafts)} drafts written on the phone")
        if not held:
            for d in drafts:
                mark = max(mark, d.get("at") or "")
    return mark


def merge_drafts(drafts: list) -> int:
    """Fold phone-written drafts into this machine's board state.

    The board keeps a draft, the angle it was built from and any pasted post in
    `state.json`, keyed by post id, and the shortlist handler reads them back
    onto the card. So this writes there rather than into the store: it is the
    same place the desktop board would have put it.
    """
    state_file = REDDIT / "state.json"
    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
    except Exception as e:
        print(f"  ! could not read the board state ({e}), drafts left in the cloud")
        return 0
    wrote = 0
    for d in drafts:
        pid = d.get("post_id")
        if not pid:
            continue
        entry = state.get(pid) or {}
        for field in ("draft", "angle", "post_text"):
            v = d.get(field)
            # Only ever fills in or replaces with something non-empty. A blank
            # from the phone must not wipe a draft written at the desk.
            if v:
                entry[field] = v
        state[pid] = entry
        wrote += 1
    try:
        tmp = state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(state_file)
    except Exception as e:
        print(f"  ! could not write the board state ({e})")
        return 0
    return wrote


def board() -> dict:
    """The whole of what the phone board shows, computed here.

    None of this is recomputed in the cloud. `shortlist.build` weighs the
    judge's verdict against freshness and room to reply, caps how many come
    from one subreddit and spends a slice of the day somewhere new;
    `new_communities` reads the whole history of what each subreddit has been
    worth. Reimplementing either at the edge would be two rankings drifting
    apart, and the phone would quietly be showing a different day than the
    laptop. So the Mac computes and the cloud holds the answer, which is the
    same rule `queue` has always followed.

    Never fatal. A board that cannot be computed costs the phone its deck for an
    hour; a collector that dies trying costs it everything.
    """
    out = {}
    try:
        sys.path.insert(0, str(REDDIT))
        import shortlist
        con = store.connect()
        # Deeper and wider than the phone shows by default. The five hour
        # rule is enforced on the PHONE, not here, so that working through the
        # fresh ones and then asking for more is instant and needs no push.
        got = shortlist.build(con, want=40, hours=48)
        asked = store.asked_authors(con) if asks_ready() else {}
        state = {}
        sfile = REDDIT / "state.json"
        if sfile.exists():
            try:
                state = json.loads(sfile.read_text())
            except Exception:
                state = {}
        for t in got["picks"]:
            st = state.get(t["id"], {})
            # `rank` is shortlist's score. In the cloud `rank` is the position in
            # the day and the score is `rank_score`, because the phone sorts by
            # position and the score is only kept for the record.
            t["rank_score"] = t.pop("rank", None)
            t["fit"] = t["rank_score"]
            t["draft"] = st.get("draft", "")
            t["angle"] = st.get("angle", "")
            # The real post if `bodies.py` has it, then anything pasted at the
            # desk, then nothing. The snippet travels separately and the board
            # falls back to it on its own.
            t["post_text"] = t.get("post_text") or st.get("post_text", "")
            t["comments"] = st.get("comments", [])
            a = asked.get((t.get("author") or "").lower())
            t["asked_at"] = a["at"] if a else None
        out["picks"] = got["picks"]
        out["counts"] = got["counts"]
        out["bar"] = got["bar"]
        out["window_hours"] = got["window_hours"]
        out["widened"] = got.get("widened")
        out["freshest_hours"] = got.get("freshest_hours")
        out["new_communities"] = shortlist.new_communities(con)
        out["people"] = store.prospects(con)
        if asks_ready():
            out["asks"] = store.ask_stats(con)
        out["health"] = store.health(con)
        # Where the voice set stands, so the phone can show it too. The table it
        # counts lives only on a Mac, so the phone cannot work it out and has to
        # be told.
        try:
            import voice as _voice          # REDDIT is already on sys.path above
            out["voice"] = _voice.stats(con)
        except Exception as e:
            print(f"  ! voice count not carried up ({e})")
        con.close()
        bodies = sum(1 for t in out["picks"]
                     if len((t.get("post_text") or "").strip()) >= 330)
        print(f"  the day's list: {len(out['picks'])} picks, {bodies} with the "
              f"whole post, {len(out['people'])} saved people")
    except Exception as e:
        print(f"  ! the day's list was not carried up ({e}), the phone keeps the last one")
    return out


def push() -> int:
    """Replace the cloud's queue with what this machine currently believes."""
    con = store.connect()
    threads = monitor.dedupe_crossposts(
        monitor.build_queue(con, monitor.all_lanes(), 1.0))
    con.close()

    generated = None
    qfile = REDDIT / "queue.json"
    if qfile.exists():
        try:
            generated = json.loads(qfile.read_text()).get("generated")
        except Exception:
            pass

    # Keep the deployed phone view in step with the one this Mac serves, so the
    # next `wrangler deploy` cannot ship a stale fork of it. Copying is not
    # deploying: the file only reaches the edge on the next deploy.
    #
    # Only on the machine that actually deploys. A collector has no `public/`,
    # because it needs no Cloudflare credentials, and creating one there would
    # invent a second copy of the phone view that nothing ever ships.
    #
    # RE-ENABLED 2026-10-17, deliberately, because the two views are one file
    # again. It had been switched off that morning for a real reason: they had
    # forked into builds for two different servers, the cockpit's reading
    # d.picks / d.counts / d.people and calling /ask /full /revise, the Worker's
    # reading d.threads and implementing none of that, so the copy would have
    # given the phone an empty board and a handful of 404s.
    #
    # What changed is that the Worker now answers that whole set. The last thing
    # the two views disagreed about was what the SERVER can do, which the page
    # used to infer from `window.isSecureContext`: a question about the browser,
    # not about the server, and it answered yes at the edge where there is no
    # transcriber. The page now asks, in `can_transcribe` and
    # `can_fetch_threads`, and one file is correct in both places.
    #
    # If they ever fork again, switch this off again rather than letting a copy
    # break the phone.
    src = HERE.parent / "cockpit" / "web" / "m.html"
    dst = HERE / "public" / "m.html"
    if dst.parent.is_dir() and src.exists():
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            dst.write_bytes(src.read_bytes())
            print(f"  staged the phone view ({dst.stat().st_size} bytes). "
                  f"It reaches the edge on the next `npx wrangler deploy`.")

    # Replies travel with the queue. They are joined to the post title here
    # rather than in the Worker, because the Mac is the only side that holds
    # posts a thread you already acted on.
    replies = []
    try:
        con = store.connect()
        con.executescript(
            "CREATE TABLE IF NOT EXISTS replies (id TEXT PRIMARY KEY, post_id TEXT,"
            " subreddit TEXT, author TEXT, body TEXT, url TEXT, created_at TEXT,"
            " first_seen TEXT, acknowledged INTEGER DEFAULT 0);")
        # PRIVATE MESSAGES STAY ON THE MAC. Everything else in the replies table
        # is already public on Reddit: a comment reply, a post reply or a
        # mention is readable by anyone with the link. A DM is not, and copying
        # one into D1 would put private correspondence on a board that is
        # reachable with a URL and a key. The cloud half gets the public half.
        #
        # Guarded on the column existing, because a store that has not yet run
        # the newer replies.py has no `kind` and the query would otherwise fail
        # and drop every reply with a one-line warning.
        has_kind = any(r[1] == "kind"
                       for r in con.execute("PRAGMA table_info(replies)"))
        clauses = []
        if has_kind:
            clauses.append("COALESCE(r.kind, 'comments') != 'messages'")
        # AutoModerator is never someone to answer. The post-replies feed drags
        # in years of bot notices on old threads, and two of those were about to
        # land on the phone next to real people who had written in.
        clauses.append("LOWER(COALESCE(r.author, '')) NOT IN "
                       "('automoderator', 'reddit', '[deleted]')")
        where = " WHERE " + " AND ".join(clauses)
        rows = con.execute(
            "SELECT r.id, r.post_id, r.subreddit, r.author, r.body, r.url,"
            " r.created_at, COALESCE(r.acknowledged, 0) AS acknowledged,"
            " p.title AS post_title, p.url AS post_url,"
            " COALESCE(p.post_text, p.snippet) AS post_snippet"
            " FROM replies r LEFT JOIN posts p ON p.id = r.post_id"
            + where +
            " ORDER BY r.created_at DESC LIMIT 200").fetchall()
        replies = [dict(x) for x in rows]
        # His own comment is the other half of the conversation, and the drafter
        # needs it or it writes an opening line at someone mid-thread. It lives
        # in the board's state file under the POST id, which is what the reply
        # joins on, and `asks` says who has already been asked to try the app.
        asked = store.asked_authors(con) if asks_ready() else {}
        state = {}
        sfile = REDDIT / "state.json"
        if sfile.exists():
            try:
                state = json.loads(sfile.read_text())
            except Exception:
                state = {}
        for r in replies:
            st = state.get(r.get("post_id") or "", {})
            r["my_comment"] = st.get("draft", "")
            a = asked.get((r.get("author") or "").lower())
            r["asked_at"] = a["at"] if a else None
            r["ask_outcome"] = a["outcome"] if a else None
        con.close()
    except Exception as e:
        print(f"  ! replies not carried up ({e})")

    res = call("/api/reddit/queue", {
        "threads": threads,
        "replies": replies,
        "generated": generated,
        "persona_system": persona_system(),
        "personas": personas(),
        "survey_url": survey_url(),
        "pushed_by": socket.gethostname(),
        **board(),
    })
    said = [f"{res.get('threads', 0)} threads"]
    for name in ("picks", "people", "replies", "communities"):
        got = res.get(name)
        if got:
            said.append(f"{got} {name}")
    print("  pushed " + ", ".join(said))
    return int(res.get("threads", 0))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pull", action="store_true", help="decisions down only")
    ap.add_argument("--push", action="store_true", help="queue up only")
    a = ap.parse_args()
    if a.pull or not a.push:
        print("pulling decisions from the phone")
        pull()
    if a.push or not a.pull:
        print("pushing the queue up")
        push()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
