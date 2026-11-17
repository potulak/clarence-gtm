#!/usr/bin/env python3
"""Your own voice, as evidence: what you actually sent, not what was drafted for you.

The drafter can be told who you are in prose, and it is, but prose about a voice
only goes so far. A model copies a voice from examples of it far better than from
any description of one, which is why this exists and why it is small.

What counts as evidence is the narrow part. A reply has four possible histories:

  - he dictated an angle, which is his words, rough, out loud
  - the model drafted something
  - he revised it by asking for a change, which is still the model's words
  - he edited the box himself, and sent that

Only the last is a sample of him writing a Reddit comment, so only the last is
used to teach the voice. A draft he accepted untouched says he approved it, which
is worth recording and is not the same thing: train on those and the model learns
to imitate itself, which is how a voice gets laundered back into the thing that
generated it. They are counted separately here and kept out of the examples.

The number to aim for is fifteen. Eight is where the generic tone goes away,
fifteen covers the kinds of reply actually written, and past about thirty the
prompt stops getting better and only gets longer.

Stdlib only. Run it for a progress report:

    python3 reddit/collector/voice.py               # where the set stands
    python3 reddit/collector/voice.py --examples    # the block the drafter gets
    python3 reddit/collector/voice.py --loose-ends  # worked, never resolved
    python3 reddit/collector/voice.py --posted <id>...   # yes, those went out
    python3 reddit/collector/voice.py --skip <id>...     # no, drop them
    python3 reddit/collector/voice.py --mine         # list ones with no provenance
    python3 reddit/collector/voice.py --mine all     # yes, those words are mine
    python3 reddit/collector/voice.py --backfill    # recover what the card kept
    python3 reddit/collector/voice.py --export      # jsonl, for a later fine-tune
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store                                      # noqa: E402

# Where the prompt stops improving. Past this you pay tokens for nothing, and a
# long wall of examples starts to crowd out the thread being answered.
TARGET = 15
MAX_IN_PROMPT = 10
# How many to show as a before and after rather than on their own. The rewrite
# pairs are the only place the model can see what gets CUT, which is half of a
# voice, but they cost twice the tokens, so a few go a long way.
# And how many to show as something he said out loud next to the comment he
# accepted for it. Fewer, because speech is long and the lesson in it is narrow.
MAX_PAIRS = 3
MAX_SPOKEN = 2
# A dictated angle runs to three thousand characters sometimes, and most of the
# voice in it is in the first few sentences. The rest is him circling back.
SPOKEN_CHARS = 600
# Shorter than this is a correction, not a comment, and teaches nothing about
# how he writes at length.
MIN_WORDS = 15
# And longer than this is an essay. One of his real replies ran to four hundred
# words, which is a fine comment and a poor example: held up as the voice to
# match it argues for length, and length is the thing most likely to make a
# drafted reply read as written rather than said.
MAX_EXAMPLE_CHARS = 1200
# A ceiling on the whole block, so that reaching the target makes the examples
# better chosen rather than merely longer. Everything above this would be paid
# for on every single draft, and the thread being answered has to stay the
# loudest thing in the prompt.
BLOCK_BUDGET = 5000


def _rows(con, post_id: str) -> list:
    return list(con.execute(
        "SELECT * FROM voice WHERE post_id = ? ORDER BY id", (post_id,)))


def episodes(con) -> list[dict]:
    """One per thread that was written in, newest first, with its history read.

    An episode is the unit worth thinking about: a thread, what the model offered
    for it, what went out, and whether those are the same text.
    """
    ids = [r["post_id"] for r in con.execute(
        "SELECT post_id, MAX(id) AS last FROM voice GROUP BY post_id "
        "ORDER BY last DESC")]
    out = []
    for pid in ids:
        rows = _rows(con, pid)
        sent = [r for r in rows if r["step"] in ("final", "edit")]
        model = [r for r in rows if r["step"] in store._MODEL_STEPS]
        angle = [r for r in rows if r["step"] == "angle"]
        if not sent:
            continue
        last = sent[-1]
        text = (last["text"] or "").strip()
        # Did he change it, or send what he was given? Compared against the last
        # thing the model produced, because a revise is the model's words too.
        before = (model[-1]["text"] or "").strip() if model else ""
        if last["provenance"] == "unknown":
            edited = None                 # backfilled: the history is not there
        elif not model:
            edited = True                 # he wrote it with no draft at all
        else:
            edited = text != before
        out.append({
            "post_id": pid,
            "text": text,
            "words": len(text.split()),
            "before": before,
            "edited": edited,
            "author": last["author"] or (model[-1]["author"] if model else None),
            "context": last["context"] or (model[-1]["context"] if model else None),
            "subreddit": last["subreddit"],
            "angle": (angle[-1]["text"] or "").strip() if angle else "",
            "posted": any(r["step"] == "final" for r in rows),
            "at": last["at"],
            "revisions": sum(1 for r in rows if r["step"] == "revise"),
        })
    return out


def usable(con, author: str | None = None) -> list[dict]:
    """The episodes that carry real evidence of his voice, of either kind.

    There are two, and they are not the same strength.

    WRITTEN: he changed the draft and sent his own words. The strongest thing
    there is, because the edit shows what he takes out as well as what he says.

    SPOKEN: he dictated what he wanted to say, read the draft and posted it as
    it stood. The comment is the model's, so it is no sample of his writing, but
    the angle is him talking with nothing in between, and the pair says which
    comment he judged right for what he meant. On a phone this is most of what
    actually gets produced, so refusing to count it means counting almost
    nothing, which is a worse answer than counting it carefully.

    Long enough to show a rhythm either way, and matched to the persona asked
    for when one is named, because the builder voice and the newcomer voice are
    different people and mixing them teaches neither.
    """
    out = []
    for e in episodes(con):
        if e["words"] < MIN_WORDS:
            continue
        if e["edited"]:
            out.append({**e, "kind": "written"})
        elif e["edited"] is False and len(e["angle"].split()) >= MIN_WORDS:
            out.append({**e, "kind": "spoken"})
    if author:
        out = [e for e in out if e["author"] in (None, author)]
    return out


def example_block(con, author: str | None = None,
                  limit: int = MAX_IN_PROMPT) -> str:
    """The examples, formatted for the drafter's system prompt. Empty until there
    is something real to show, because a half-empty example block is worse than
    none: one sample reads as a template to be filled in rather than a voice."""
    have = usable(con, author)
    if len(have) < 3:
        return ""
    # Outliers out first, then newest within the budget: an example earns its
    # place by being typical, not by being long.
    have = [e for e in have if len(e["text"]) <= MAX_EXAMPLE_CHARS]
    kept, spend = [], 0
    for e in have:
        cost = len(e["text"]) + (len(e["before"]) if e["before"] else 0)
        if len(kept) >= limit or spend + cost > BLOCK_BUDGET:
            continue
        kept.append(e)
        spend += cost
    have = kept
    written = [e for e in have if e["kind"] == "written"]
    spoken = [e for e in have if e["kind"] == "spoken"][:MAX_SPOKEN]
    # Pairs are the richer form, so they get first call on the budget, but never
    # all of it once there is a choice: a block of nothing but rewrites teaches
    # the edit and never shows a finished comment standing on its own.
    pairs = [e for e in written if e["before"]][:MAX_PAIRS]
    taken = {e["post_id"] for e in pairs}
    plain = [e for e in written if e["post_id"] not in taken]
    s = ("\nHOW THIS PERSON ACTUALLY WRITES. Below is their real writing. Match "
         "the rhythm, the sentence length, the way they open and the way they "
         "stop. Do NOT reuse their specifics, their stories or their phrasings: "
         "those belong to the threads they were written for.\n")
    if plain:
        s += "\nCOMMENTS THEY POSTED:\n"
        for e in plain:
            s += f"\n{e['text']}\n"
    if pairs:
        s += ("\nAND HERE IS WHAT THEY CHANGE ABOUT A DRAFT. The first is what a "
              "model gave them, the second is what they actually sent instead. "
              "The difference is the voice. Learn what they cut.\n")
        for e in pairs:
            s += f"\nDRAFT:\n{e['before']}\nTHEY SENT INSTEAD:\n{e['text']}\n"
    if spoken:
        s += ("\nAND HERE IS HIM TALKING. The first is what he said out loud "
              "about the thread, dictated and unedited, so it rambles and "
              "repeats. The second is the comment he read and decided was right "
              "for it. Take his WORDS and his position from the speech: the "
              "things he actually says, the way he hedges, what he cares about. "
              "Do NOT copy the shape of it. A comment is far tighter than "
              "speech, and the second one shows the length he accepts.\n")
        for e in spoken:
            said = e["angle"]
            if len(said) > SPOKEN_CHARS:
                said = said[:SPOKEN_CHARS].rsplit(" ", 1)[0] + "..."
            s += f"\nHE SAID:\n{said}\nAND ACCEPTED THIS:\n{e['text']}\n"
    return s


def stats(con) -> dict:
    eps = episodes(con)
    ed = [e for e in eps if e["edited"]]
    use = usable(con)
    n_angles = con.execute(
        "SELECT COUNT(DISTINCT post_id) FROM voice WHERE step = 'angle'"
    ).fetchone()[0]
    return {
        "episodes": len(eps),
        "posted": sum(1 for e in eps if e["posted"]),
        "edited": len(ed),
        "accepted_unchanged": sum(1 for e in eps if e["edited"] is False),
        "unknown": sum(1 for e in eps if e["edited"] is None),
        "usable": len(use),
        "written": sum(1 for e in use if e["kind"] == "written"),
        "spoken": sum(1 for e in use if e["kind"] == "spoken"),
        "target": TARGET,
        "to_go": max(0, TARGET - len(use)),
        "dictated_angles": n_angles,
        "median_words": (sorted(e["words"] for e in use)[len(use) // 2]
                         if use else 0),
    }


def backfill(con) -> dict:
    """Recover the replies written before any of this existed.

    The card kept the last value of its text box in `state.json`, so for a thread
    marked posted there is usually a reply sitting there. What is NOT there is
    who wrote it: that one field held the model's draft and his rewrite at
    different moments and never said which. So these come in marked `unknown`,
    which keeps them out of the examples on purpose. They are a record, not
    evidence, and labelling them as his would quietly poison the one set that has
    to stay clean.
    """
    state_file = store.DATA / "state.json"
    if not state_file.exists():
        return {"ok": False, "error": f"no state.json at {state_file}"}
    try:
        state = json.loads(state_file.read_text())
    except Exception as e:
        return {"ok": False, "error": f"state.json unreadable: {e}"}
    # `posts.status` and not `actions`. The actions table is the OUTBOUND log of
    # decisions made here, so a status that arrived by syncing the phone down is
    # not in it: `apply_action` writes the post and deliberately does not queue
    # it for pushing back. Reading actions therefore missed every reply written
    # on the phone, which is most of them.
    posted = [r[0] for r in con.execute(
        "SELECT id FROM posts WHERE status = 'posted'")]
    added, skipped = 0, 0
    for pid in posted:
        entry = state.get(pid) or {}
        text = (entry.get("draft") or "").strip()
        if not text:
            skipped += 1
            continue
        if con.execute("SELECT 1 FROM voice WHERE post_id = ? AND step = 'final'",
                       (pid,)).fetchone():
            continue
        sub = con.execute("SELECT subreddit FROM posts WHERE id = ?",
                          (pid,)).fetchone()
        if store.capture(con, pid, "final", text,
                         subreddit=sub["subreddit"] if sub else None,
                         provenance="unknown"):
            added += 1
        angle = (entry.get("angle") or "").strip()
        if angle:
            store.capture(con, pid, "angle", angle, provenance="his")
    return {"ok": True, "added": added, "no_text": skipped,
            "posted_total": len(posted)}


def loose_ends(con) -> list[dict]:
    """Threads you did the work on and never said what happened.

    Posting happens in the Reddit app, away from the board, so the tap that says
    it went out is the easiest thing in the whole loop to forget. A thread left
    like that is worse than untouched: it sits at the top of the queue looking
    like work to do, and its reply never counts as a reply.

    Only threads with real work on them, never the whole queue. Strongest signal
    first: COPYING the text is nearly posting it, since there is no other reason
    to put a reply on the clipboard, so those are asked about before the ones
    that only have a draft sitting in the box.
    """
    rows = con.execute(
        "SELECT p.id, p.subreddit, p.title, p.status, p.url, p.acted_at, "
        "       (SELECT MAX(at) FROM events e "
        "         WHERE e.post_id = p.id AND e.kind = 'copy') AS copied_at, "
        "       (SELECT MAX(at) FROM voice v WHERE v.post_id = p.id) AS worked_at, "
        "       (SELECT text FROM voice v WHERE v.post_id = p.id "
        "         ORDER BY v.id DESC LIMIT 1) AS last_text "
        "  FROM posts p "
        " WHERE COALESCE(p.status, '') NOT IN ('posted', 'skip') "
        "   AND (copied_at IS NOT NULL OR worked_at IS NOT NULL "
        "        OR p.status = 'drafted') "
        " ORDER BY (copied_at IS NULL), "
        "          COALESCE(copied_at, worked_at, p.acted_at) DESC"
    ).fetchall()
    # The threads worked before any of this existed have no voice rows at all,
    # so their drafts are only in the board's own state file. Read it as a
    # fallback: the point of the listing is to recognise the words, and showing
    # a title with no reply under it asks you to remember a thread from a month
    # ago by its headline.
    state = {}
    sfile = store.DATA / "state.json"
    if sfile.exists():
        try:
            state = json.loads(sfile.read_text())
        except Exception:
            state = {}
    out = []
    for r in rows:
        text = (r["last_text"] or "").strip()
        if not text:
            text = ((state.get(r["id"]) or {}).get("draft") or "").strip()
        out.append({
            "id": r["id"], "subreddit": r["subreddit"], "title": r["title"],
            "status": r["status"] or "todo", "url": r["url"],
            "copied": r["copied_at"],
            "worked": r["worked_at"] or r["acted_at"],
            "text": text,
        })
    return out


def loose_ends_report(con, reply_chars: int = 240) -> str:
    """The list, numbered, with enough of each reply to recognise it.

    Asking "did you post this" and offering only a post id is asking you to
    remember a hash. The words you wrote and a link to the thread are what the
    question is actually answered with, so both are here, and the numbers are so
    that saying yes to four of them is four numbers and not four ids.
    """
    rows = loose_ends(con)
    if not rows:
        return ("Nothing hanging. Every thread you worked is either posted or "
                "skipped.")
    L = [f"{len(rows)} threads you worked and never said what happened.", ""]
    copied = [r for r in rows if r["copied"]]
    if copied:
        L.append("COPIED, so these almost certainly went out:")
        L.append("")
    for i, r in enumerate(rows, 1):
        if copied and i == len(copied) + 1:
            L.append("NEVER COPIED, so probably never posted:")
            L.append("")
        # Labelled, because this is the last time the row was touched and not
        # necessarily when the reply was written: resolving one, or putting it
        # back, moves it.
        when = (r["copied"] or r["worked"] or "")[:16].replace("T", " ")
        stamp = ("copied " if r["copied"] else "last touched ") + when
        L.append(f"{i:3}. r/{r['subreddit']}  ({stamp})")
        # What you wrote comes FIRST and whole. The title went first once and a
        # title is the one thing in here that is not yours: four good replies got
        # skipped on the strength of the thread's headline alone.
        if r["text"]:
            body = " ".join(r["text"].split())
            for line in _wrap(body, 72):
                L.append(f"     {line}")
        else:
            L.append("     (no draft kept for this one)")
        L.append(f"     re: {(r['title'] or '').strip()[:70]}")
        if r["url"]:
            L.append(f"     {r['url']}")
        L.append("")
    L.append("Tell me the numbers, or run it yourself:")
    L.append("  python3 reddit/collector/voice.py --posted 1 4 9")
    L.append("  python3 reddit/collector/voice.py --skip 2 7")
    L.append("")
    L.append("Numbers are this listing's order, so resolve them before working "
             "the queue again. Post ids always work too. Read the replies before "
             "clearing any of these: they were drafted for a reason and the "
             "thread's title is not what you are judging.")
    return "\n".join(L)


def resolve_refs(con, refs: list[str]) -> tuple[list[str], list[str]]:
    """Turn what you typed into post ids: a number from the listing, `all`, or
    an id. Anything that matches nothing comes back separately rather than being
    guessed at, because the one unrecoverable mistake here is marking the wrong
    thread as posted."""
    rows = loose_ends(con)
    if refs == ["all"]:
        return [r["id"] for r in rows], []
    ids, bad = [], []
    for ref in refs:
        if ref.isdigit():
            n = int(ref)
            if 1 <= n <= len(rows):
                ids.append(rows[n - 1]["id"])
            else:
                bad.append(ref)
        elif ref.startswith("t3_"):
            ids.append(ref)
        else:
            bad.append(ref)
    return ids, bad


def resolve(con, ids: list[str], status: str) -> dict:
    """Say what happened to threads you had left hanging.

    Goes through `set_status`, the same path the board uses, so the decision
    lands in `actions` and gets pushed to the phone and taught to the subreddit
    score like any other. A reply that went out should count the same however
    late you confirm it.
    """
    done, unknown = [], []
    for pid in ids:
        if not con.execute("SELECT 1 FROM posts WHERE id = ?", (pid,)).fetchone():
            unknown.append(pid)
            continue
        store.set_status(con, pid, status, reason=None,
                         note="confirmed late, from the loose ends review")
        if status == "posted":
            last = store.last_voice(con, pid)
            if last is not None and (last["text"] or "").strip():
                store.capture(con, pid, "final", last["text"],
                              author=last["author"], force=True)
        done.append(pid)
    return {"ok": True, "marked": done, "status": status, "unknown": unknown}


def claim(con, ids: list[str] | None = None) -> dict:
    """Say, for a reply whose history was never recorded, that the words are yours.

    Needed because of one real failure. The phone decided whether a draft had
    been hand-edited by comparing the text being copied against its own stored
    draft, but the draft box saved on blur and the blur wrote the edit into that
    same stored value, so the comparison was always against itself and the answer
    was always "unchanged". Every reply written on the phone before that was
    fixed therefore carries no provenance at all.

    Guessing is the one thing not to do here: the whole set is only worth
    anything if nothing in it is assumed. But you were there, so you can simply
    say. Nothing infers this, and it is per reply, not a blanket switch, except
    where you deliberately pass `all`.
    """
    rows = con.execute(
        "SELECT post_id, id, text FROM voice WHERE step = 'final' "
        "AND provenance = 'unknown' ORDER BY id").fetchall()
    want = set(ids or [])
    took = []
    for r in rows:
        if want and r["post_id"] not in want:
            continue
        con.execute("UPDATE voice SET provenance = 'his' WHERE id = ?", (r["id"],))
        took.append(r["post_id"])
    con.commit()
    missing = sorted(want - set(took))
    return {"ok": True, "claimed": took, "not_found": missing,
            "left_unknown": len(rows) - len(took)}


def export(con, author: str | None = None) -> str:
    """The set as jsonl, one object per usable example. For the day there are
    hundreds of these and a fine-tune is worth arguing about. Today it is a way
    to read the set without sqlite."""
    return "\n".join(json.dumps({
        "post_id": e["post_id"], "subreddit": e["subreddit"],
        "author": e["author"], "context": e["context"],
        "angle": e["angle"], "draft": e["before"], "sent": e["text"],
        "words": e["words"], "at": e["at"],
    }, ensure_ascii=False) for e in usable(con, author))


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def _bar(n: int, total: int, width: int = 24) -> str:
    done = min(width, round(width * n / total)) if total else 0
    return "[" + "#" * done + "." * (width - done) + "]"


def report(con) -> str:
    st = stats(con)
    L = [f"Voice set: {st['usable']} of {st['target']} usable examples",
         f"  {_bar(st['usable'], st['target'])}  {st['to_go']} to go", ""]
    L.append(f"  threads written in       {st['episodes']}")
    L.append(f"  marked posted            {st['posted']}")
    L.append(f"  you edited the draft     {st['written']}   "
             "<- strongest, your own words")
    L.append(f"  you dictated and posted  {st['spoken']}   "
             "<- counts too, the angle is you talking")
    L.append(f"  posted, nothing said     {st['accepted_unchanged'] - st['spoken']}"
             "   (no angle and no edit, so nothing to learn from)")
    L.append(f"  provenance unknown       {st['unknown']}   "
             "(backfilled, kept out of the examples)")
    L.append(f"  angles you dictated      {st['dictated_angles']}")
    if st["usable"]:
        L.append(f"  median length            {st['median_words']} words")
    L.append("")
    if st["usable"] < 3:
        L.append("The drafter is not using examples yet. It starts at 3, because one "
                 "example reads as a template to fill in rather than a voice.")
    elif st["to_go"]:
        L.append(f"The drafter is using {min(st['usable'], MAX_IN_PROMPT)} of them "
                 f"now. {st['to_go']} more and the voice is as good as a prompt "
                 "can make it.")
    else:
        L.append("Target reached. The drafter has what it needs; more helps a little, "
                 "and past about 30 it stops helping at all.")
    return "\n".join(L)


def main(argv: list[str]) -> int:
    con = store.connect()
    if "--backfill" in argv:
        out = backfill(con)
        if not out.get("ok"):
            print(out.get("error"))
            return 1
        print(f"Recovered {out['added']} reply/replies from state.json "
              f"({out['posted_total']} threads marked posted, "
              f"{out['no_text']} had no text kept).")
        print("Marked `unknown`, so they stay out of the examples. They are a "
              "record of what went out, not evidence of how you write.")
        print()
        print(report(con))
        return 0
    if "--examples" in argv:
        author = next((a.split("=", 1)[1] for a in argv
                       if a.startswith("--author=")), None)
        block = example_block(con, author)
        print(block if block else
              "No examples yet. Needs 3 edited replies before the drafter uses any.")
        return 0
    if "--export" in argv:
        print(export(con))
        return 0
    if "--loose-ends" in argv or "--loose" in argv:
        print(loose_ends_report(con))
        return 0
    if "--mine" in argv:
        rest = [a for a in argv[argv.index("--mine") + 1:]
                if not a.startswith("--")]
        if not rest:
            print("Which replies are yours? Give post ids, or `--mine all`.")
            print("These have no recorded provenance:")
            for r in con.execute(
                    "SELECT v.post_id, p.subreddit, LENGTH(v.text) AS n "
                    "FROM voice v LEFT JOIN posts p ON p.id = v.post_id "
                    "WHERE v.step = 'final' AND v.provenance = 'unknown' "
                    "ORDER BY v.id"):
                print(f"  {r['post_id']:13} r/{r['subreddit'] or '?':20} "
                      f"{r['n']} chars")
            return 1
        out = claim(con, None if rest == ["all"] else rest)
        print(f"Taken as your words: {len(out['claimed'])}")
        if out["not_found"]:
            print("No unknown final for: " + ", ".join(out["not_found"]))
        print()
        print(report(con))
        return 0
    for flag, status in (("--posted", "posted"), ("--skip", "skip")):
        if flag in argv:
            refs = [a for a in argv[argv.index(flag) + 1:]
                    if not a.startswith("--")]
            if not refs:
                print(f"Which ones? {flag} 1 4 9, or {flag} all, or "
                      f"{flag} t3_abc123")
                print()
                print(loose_ends_report(con))
                return 1
            ids, bad = resolve_refs(con, refs)
            if bad:
                print("Not a number in the list or a post id, so nothing was "
                      "touched: " + ", ".join(bad))
                return 1
            # Clearing a batch in one word is how four good replies went in the
            # bin: the drafts were right there and the decision got made on the
            # thread titles. A blanket sweep over threads that HAVE replies
            # written now has to be asked for twice.
            if refs == ["all"] and "--yes" not in argv:
                withtext = [e for e in loose_ends(con)
                            if e["id"] in ids and e["text"]]
                if withtext:
                    print(f"{len(ids)} threads, and {len(withtext)} of them have "
                          f"a reply already written.")
                    print("Those took work and they are not judged by the thread "
                          "title, so read them first:")
                    print("  python3 reddit/collector/voice.py --loose-ends")
                    print(f"Then name the ones you mean, or repeat this with "
                          f"--yes if you really do mean all of them:")
                    print(f"  python3 reddit/collector/voice.py {flag} all --yes")
                    return 1
            out = resolve(con, ids, status)
            if out["marked"]:
                print(f"Marked {len(out['marked'])} as {status}: "
                      + ", ".join(out["marked"]))
            if out["unknown"]:
                print("Not in the store, so left alone: "
                      + ", ".join(out["unknown"]))
            print()
            print(report(con))
            return 0
    print(report(con))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
