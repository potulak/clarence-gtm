#!/usr/bin/env python3
"""Trade data with the cloud board over HTTPS.

    the collector  owns posts, scores, verdicts  ->  pushed up, replacing `queue`
    the cloud      owns your decisions           ->  pulled down, into `posts`

Neither side writes the other's columns, so there is nothing to merge and no
conflict to resolve. This is the same split `sync-cloud.py` uses between the Air
and the cloud, lifted so a runner can stand where a laptop used to and keeping
the one rule that makes it safe.

PULL RUNS FIRST, always. Pushing first would republish a post you triaged on the
phone as though it were still waiting, and you would see it again.

Talks to the Worker with the cockpit key rather than wrangler, because wrangler
authenticates per machine with its own OAuth login. The key is the only
credential needed, which is what lets any machine be the collector.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import store            # noqa: E402
import monitor          # noqa: E402

BOARD = (os.environ.get("CLARENCE_BOARD_URL")
         or "https://clarence-reddit.<your-subdomain>.workers.dev").rstrip("/")

# The laptop keeps the key in the same place the cockpit and the AEO tools do.
# A runner has no home directory worth writing to, so it passes COCKPIT_KEY.
KEY_FILE = (pathlib.Path.home() / "Library" / "Application Support"
            / "clarence-aeo" / "cockpit-key")


class CloudError(RuntimeError):
    """The board could not be reached or refused us. Never silent."""


def key() -> str:
    env = (os.environ.get("COCKPIT_KEY") or "").strip()
    if env:
        return env
    if KEY_FILE.exists():
        k = KEY_FILE.read_text().strip()
        if k:
            return k
    raise CloudError(
        "no cockpit key. Set the COCKPIT_KEY secret, or on a laptop put it at "
        f"{KEY_FILE}")


def call(path: str, payload: dict | None = None, timeout: int = 120) -> dict:
    """One request to the board. Raises rather than exiting, so a caller can
    decide whether a failed sync should take the whole run down."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BOARD}{path}", data=data,
        headers={"Content-Type": "application/json", "Cookie": f"ck={key()}",
                 # Cloudflare answers 1010 to urllib's default agent, which reads
                 # as a bot signature. Naming ourselves honestly gets us through
                 # and makes the traffic identifiable in the logs.
                 "User-Agent": "clarence-collector/1.0 (+reddit cockpit)"},
        method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        # Deliberately does not echo the key or the headers. A 403 here means the
        # key is wrong, and the useful thing to print is that fact, not the key.
        raise CloudError(f"board said HTTP {e.code} for {path}: {body}") from None
    except Exception as e:
        raise CloudError(f"could not reach the board ({e})") from None


def pull(con) -> dict:
    """Bring decisions down from the phone and replay them into the store.

    Asks for the WHOLE journal every time rather than tracking a watermark. It
    costs one request over a couple of hundred rows, and `apply_action` is
    idempotent by construction: it compares the time the decision was MADE and
    refuses to move a status that is already current or newer. A watermark would
    buy nothing here and would be one more piece of state for an ephemeral
    runner to lose, which is how a decision gets skipped forever.
    """
    res = call("/api/reddit/actions?since=")
    rows = res.get("actions") or []
    applied = 0
    for r in rows:
        if store.apply_action(con, r["post_id"], r["status"],
                              r.get("reason") or None, r.get("note") or None,
                              r["at"]):
            applied += 1
    # apply_action declines for two different reasons, and conflating them would
    # hide a real problem: a post this machine never collected looks exactly like
    # a decision that is already current here.
    return {"seen": len(rows), "applied": applied,
            "already_current_or_unknown": len(rows) - applied}


def push(con) -> dict:
    """Replace the cloud's queue with what this machine currently believes.

    Sends the FULL ranked pool (top_pct 1.0), not a slice, because the Worker
    replaces the queue table wholesale on every push. Sending a slice would
    quietly shorten the phone's backlog to whatever this run happened to rank.

    Deliberately sends no `persona_system` and no `replies`. Both live on the
    Mac: the persona comes out of `enrich`, and replies out of `replies.py`,
    neither of which a runner has. The Worker skips null fields on ingest, so
    leaving them out preserves whatever the Mac last pushed instead of blanking
    it. Verified against handleIngest in scratch/cloud/src/worker.js.
    """
    threads = monitor.dedupe_crossposts(
        monitor.build_queue(con, monitor.all_lanes(), 1.0))
    res = call("/api/reddit/queue", {
        "threads": threads,
        "generated": store.now(),
        "pushed_by": os.environ.get("CLARENCE_ORIGIN") or socket.gethostname(),
    })
    return {"threads": int(res.get("threads", 0))}
