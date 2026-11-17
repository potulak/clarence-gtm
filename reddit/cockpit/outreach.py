"""The First-10-users tracker: hand-recruiting made legible.

The strategy at zero users is not more traffic, it is ~10 real people recruited
one at a time from where the pain already lives (a Reddit thread, an X post, a
TikTok comment about losing touch with friends). This module is the ledger for
that push: every post you engage becomes a row, and every row carries a tagged
invite link so, when someone signs up, the attribution engine can tell you which
outreach actually worked. That closes the loop between "I commented here" and "a
user came from it."

Deliberately small. It tracks conversations, not a CRM. Stdlib only, JSON store.
"""
# `update()` returns `dict | None`, which macOS system Python (3.9, what the
# always-on Air runs) evaluates at import time and chokes on. Without this the
# module raises on import over there, the cockpit's lazy _outreach() swallows it,
# and the First-10 tab is quietly missing on the one machine that is always up.
from __future__ import annotations

import json
import secrets
import datetime
from pathlib import Path
from urllib.parse import urlparse


def _data_dir() -> Path:
    """Where the ledger sits, which is not where this code sits.

    This module is version controlled now. The ledger is not, and must not be:
    every row is a real person, their handle and what was said to them. So code
    and data part company here, the way store._data_dir does for the collector.

    Worked out from this file's position rather than from a variable someone has
    to remember to set, because the failure mode is quiet: a ledger resolving to
    a path with no file reads as "nobody recruited yet" and starts a fresh one,
    on top of writing those handles into the repo.
    """
    here = Path(__file__).resolve().parent
    if here.name == "cockpit" and here.parent.name == "reddit":
        return here.parents[1] / "scratch" / "cockpit"
    return here                       # still running from the old location


STORE = _data_dir() / "outreach.json"

GOAL = 10  # the only number that matters right now

# The funnel, in order. Each row moves left to right (or falls to "passed").
STATUSES = ["spotted", "engaged", "talking", "invited", "user", "passed"]
STATUS_LABEL = {
    "spotted": "Spotted",       # found the post, not engaged yet
    "engaged": "Engaged",       # commented / replied / DM'd
    "talking": "In conversation",  # they replied, it is a real back-and-forth
    "invited": "Invited",       # sent them Clarence (the tagged link)
    "user": "User",             # installed and using it
    "passed": "Passed",         # dead end, no fit, no response
}
# What counts toward the 10.
COUNTS_AS_USER = {"user"}


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def platform_of(url: str) -> str:
    host = ""
    try:
        host = (urlparse(url).hostname or "").lower().replace("www.", "")
    except Exception:
        host = ""
    table = [
        ("reddit.com", "reddit"),
        ("x.com", "x"), ("twitter.com", "x"), ("t.co", "x"),
        ("tiktok.com", "tiktok"),
        ("news.ycombinator.com", "hn"),
        ("instagram.com", "instagram"),
        ("facebook.com", "facebook"),
        ("threads.net", "threads"),
        ("linkedin.com", "linkedin"),
        ("discord", "discord"),
        ("youtube.com", "youtube"),
    ]
    for pat, name in table:
        if pat in host:
            return name
    return host or "other"


def invite_link(platform: str, campaign: str) -> str:
    return (f"https://getclarence.app/?utm_source={platform}"
            f"&utm_medium=outreach&utm_campaign={campaign}")


def _load() -> list:
    if not STORE.exists():
        return []
    try:
        return json.loads(STORE.read_text())
    except Exception:
        return []


def _save(rows: list) -> None:
    STORE.write_text(json.dumps(rows, indent=2))


def add(url="", handle="", signal="", action="", message="",
        warm=False, notes="") -> dict:
    rows = _load()
    plat = platform_of(url)
    sid = secrets.token_hex(3)
    campaign = f"or-{plat}-{sid}"
    # Whether we have engaged yet decides the starting funnel stage.
    status = "engaged" if (action or message) else "spotted"
    entry = {
        "id": sid,
        "created": _now(),
        "url": url.strip(),
        "platform": plat,
        "handle": handle.strip(),
        "signal": signal.strip(),
        "action": action.strip(),
        "message": message.strip(),
        "warm": bool(warm),
        "status": status,
        "campaign": campaign,
        "invite_link": invite_link(plat, campaign),
        "notes": notes.strip(),
    }
    rows.append(entry)
    _save(rows)
    return entry


def update(entry_id, **fields) -> dict | None:
    rows = _load()
    for e in rows:
        if e["id"] == entry_id:
            for k in ("status", "handle", "signal", "action", "message",
                      "notes", "warm", "url"):
                if k in fields:
                    e[k] = fields[k]
            # If the url changed, re-derive platform + keep the link honest.
            if "url" in fields:
                e["platform"] = platform_of(e["url"])
                e["invite_link"] = invite_link(e["platform"], e["campaign"])
            _save(rows)
            return e
    return None


def remove(entry_id) -> bool:
    rows = _load()
    kept = [e for e in rows if e["id"] != entry_id]
    if len(kept) == len(rows):
        return False
    _save(kept)
    return True


def view() -> dict:
    rows = _load()
    funnel = {s: 0 for s in STATUSES}
    for e in rows:
        funnel[e.get("status", "spotted")] = funnel.get(e.get("status", "spotted"), 0) + 1
    users = sum(1 for e in rows if e.get("status") in COUNTS_AS_USER)
    warm_users = sum(1 for e in rows if e.get("status") in COUNTS_AS_USER and e.get("warm"))
    # newest first
    rows_sorted = sorted(rows, key=lambda e: e.get("created", ""), reverse=True)
    return {
        "goal": GOAL,
        "users": users,
        "warm_users": warm_users,
        "cold_users": users - warm_users,
        "remaining": max(0, GOAL - users),
        "funnel": funnel,
        "statuses": STATUSES,
        "status_label": STATUS_LABEL,
        "entries": rows_sorted,
    }
