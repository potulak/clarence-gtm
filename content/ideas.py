#!/usr/bin/env python3
"""The content idea backlog: a persistent, editable pipeline of things to write.

This is the layer above the auto /answers pipeline. Where generate.py picks one
gap question per run and ships a short answer page, the backlog is a standing
list you curate: guide ideas, blog-post ideas, angles worth owning, each tracked
from idea to published. It lives in a TRACKED file (content/ideas.json) so the
laptop cockpit and the cloud both read the same source of truth.

An idea:
  {
    "id":            slug, stable, dedup key
    "title":         the working headline
    "angle":         one line on the thesis / what makes it worth writing
    "kind":          "guide" | "blog" | "answer"
    "source":        "manual" | "aeo-gap" | "reddit" | "icp"
    "source_ref":    the query text or the Reddit URL it came from
    "status":        "idea" | "drafting" | "in-pr" | "published"
    "pr_url":        set once a draft PR is open
    "published_url": set once merged and live
    "created":       YYYY-MM-DD
    "notes":         free text
  }

Status flow: idea -> drafting -> in-pr -> published. Nothing here writes or
posts anything: drafting a card runs longform.py, which opens a PR the founder
merges. The merge is still the gate.

CLI:
  python3 ideas.py list                 # print the backlog
  python3 ideas.py seed                 # fill from AEO gaps + Reddit + ICP
  python3 ideas.py add "Title" --kind guide --angle "..."
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import generate as gen   # reuse: slugify, absent_queries, written_slugs, on_topic

IDEAS = HERE / "ideas.json"
REPO = HERE.parent
REDDIT_QUEUE = REPO / "scratch" / "reddit" / "queue.json"

STATUSES = ("idea", "drafting", "in-pr", "published")
KINDS = ("guide", "blog", "answer")

# How many to pull from each source in one seed pass, so a single click never
# floods the board. The board is meant to stay readable and curated.
SEED_CAP = {"aeo-gap": 8, "reddit": 6, "icp": 4}


# --------------------------------------------------------------- store
def load() -> dict:
    if IDEAS.exists():
        try:
            data = json.loads(IDEAS.read_text())
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("ideas", [])
    return data


def save(data: dict) -> None:
    data.setdefault("ideas", [])
    IDEAS.write_text(json.dumps(data, indent=2) + "\n")


def _ids(data: dict) -> set[str]:
    return {i.get("id", "") for i in data["ideas"]}


def get(idea_id: str) -> dict | None:
    return next((i for i in load()["ideas"] if i.get("id") == idea_id), None)


def add(title: str, angle: str = "", kind: str = "guide",
        source: str = "manual", source_ref: str = "", notes: str = "",
        data: dict | None = None) -> dict | None:
    """Add one idea. Dedup by slug id; returns the idea, or None if it already
    exists (or the title is empty). When `data` is passed the caller batches and
    saves once (used by seed); otherwise this reads, appends, and saves."""
    title = (title or "").strip()
    if not title:
        return None
    standalone = data is None
    if standalone:
        data = load()
    idea_id = gen.slugify(title)
    if not idea_id or idea_id in _ids(data):
        return None
    if kind not in KINDS:
        kind = "guide"
    idea = {
        "id": idea_id,
        "title": title,
        "angle": angle.strip(),
        "kind": kind,
        "source": source,
        "source_ref": source_ref,
        "status": "idea",
        "pr_url": "",
        "published_url": "",
        "created": date.today().isoformat(),
        "notes": notes.strip(),
    }
    data["ideas"].append(idea)
    if standalone:
        save(data)
    return idea


def update(idea_id: str, **fields) -> dict | None:
    """Patch an idea in place. Only known keys are written. Status is validated."""
    data = load()
    idea = next((i for i in data["ideas"] if i.get("id") == idea_id), None)
    if not idea:
        return None
    allowed = {"title", "angle", "kind", "source", "source_ref",
               "status", "pr_url", "published_url", "notes"}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "status" and v not in STATUSES:
            continue
        if k == "kind" and v not in KINDS:
            continue
        idea[k] = v
    save(data)
    return idea


def remove(idea_id: str) -> bool:
    data = load()
    before = len(data["ideas"])
    data["ideas"] = [i for i in data["ideas"] if i.get("id") != idea_id]
    save(data)
    return len(data["ideas"]) < before


# --------------------------------------------------------------- seed
def _seen_keys(data: dict) -> set[str]:
    """Everything we should not re-propose: existing backlog ids, plus the slugs
    of answer pages already written (so a gap we've already answered short does
    not reappear as a fresh idea)."""
    seen = set(_ids(data))
    try:
        seen |= {gen.slugify(q) for q in gen.written_slugs()}
        seen |= set(gen.written_slugs())
    except Exception:
        pass
    return seen


def _seed_from_gaps(data: dict, seen: set[str]) -> int:
    added = 0
    try:
        gaps = gen.absent_queries()
    except Exception:
        gaps = []
    for q in gaps:
        if added >= SEED_CAP["aeo-gap"]:
            break
        if gen._branded(q) or not gen.on_topic(q):
            continue
        if gen.slugify(q) in seen or q in seen:
            continue
        idea = add(q, angle="Answer-engine gap: own this query with a full guide.",
                   kind="guide", source="aeo-gap", source_ref=q, data=data)
        if idea:
            seen.add(idea["id"])
            added += 1
    return added


def _seed_from_reddit(data: dict, seen: set[str]) -> int:
    if not REDDIT_QUEUE.exists():
        return 0
    try:
        threads = json.loads(REDDIT_QUEUE.read_text()).get("threads", [])
    except Exception:
        return 0
    # Best-fit threads first, so the strongest signals seed real posts.
    threads = sorted(threads, key=lambda t: t.get("fit", 0), reverse=True)
    added = 0
    for t in threads:
        if added >= SEED_CAP["reddit"]:
            break
        title = (t.get("title") or "").strip()
        if not title or gen.slugify(title) in seen:
            continue
        cluster = t.get("matched_cluster", "")
        angle = (f"Real thread in r/{t.get('subreddit','')} "
                 f"({cluster}): write the post that speaks to this.")
        idea = add(title, angle=angle, kind="blog", source="reddit",
                   source_ref=t.get("url", ""), data=data)
        if idea:
            seen.add(idea["id"])
            added += 1
    return added


def _seed_from_icp(data: dict, seen: set[str]) -> int:
    corpus = gen.CORPUS
    if not corpus or not corpus.exists():
        return 0
    pains: list[str] = []
    for line in corpus.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        pains += r.get("pain_points", [])
    pains = list(dict.fromkeys(pains))       # dedup, keep order
    added = 0
    for pain in pains:
        if added >= SEED_CAP["icp"]:
            break
        title = pain.strip()
        if not title or gen.slugify(title) in seen:
            continue
        idea = add(title[:90], angle="ICP pain in their own words: a post that names it.",
                   kind="blog", source="icp", source_ref="icp-corpus", data=data)
        if idea:
            seen.add(idea["id"])
            added += 1
    return added


def seed() -> dict:
    """Fill the backlog from the signals we already collect: AEO gaps (guides),
    live Reddit threads (blog posts), and ICP pains (blog posts). Deduped against
    the board and against answers already written. Saves once."""
    data = load()
    seen = _seen_keys(data)
    by_source = {
        "aeo-gap": _seed_from_gaps(data, seen),
        "reddit": _seed_from_reddit(data, seen),
        "icp": _seed_from_icp(data, seen),
    }
    save(data)
    return {"added": sum(by_source.values()), "by_source": by_source,
            "total": len(data["ideas"])}


# --------------------------------------------------------------- CLI
def _cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    if cmd == "seed":
        res = seed()
        print(json.dumps(res, indent=2))
        return 0
    if cmd == "add":
        if len(argv) < 2:
            print("usage: ideas.py add \"Title\" [--kind guide|blog|answer] "
                  "[--angle \"...\"] [--source manual]")
            return 2
        title = argv[1]
        opt = lambda name, dflt: (argv[argv.index(name) + 1]
                                  if name in argv else dflt)
        idea = add(title, angle=opt("--angle", ""), kind=opt("--kind", "guide"),
                   source=opt("--source", "manual"),
                   source_ref=opt("--ref", ""), notes=opt("--notes", ""))
        print(json.dumps(idea, indent=2) if idea
              else "not added (empty or duplicate).")
        return 0 if idea else 1
    # default: list
    data = load()
    if not data["ideas"]:
        print("backlog empty. run: python3 ideas.py seed")
        return 0
    by_status: dict[str, list] = {s: [] for s in STATUSES}
    for i in data["ideas"]:
        by_status.setdefault(i.get("status", "idea"), []).append(i)
    for s in STATUSES:
        items = by_status.get(s, [])
        if not items:
            continue
        print(f"\n{s.upper()} ({len(items)})")
        for i in items:
            print(f"  [{i['kind']:5}] {i['title']}  ·  {i.get('source','')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
