#!/usr/bin/env python3
"""Manual Reddit draft-assist. No API, no scraping, no posting.

Reddit closed self-service API access, and automation is exactly what gets
accounts banned, so this tool never fetches Reddit and never posts. You read
Reddit yourself, paste the thread you want to answer (the post, plus any comments
you want it to see), optionally add your own angle by voice or text, and it hands
you a genuine draft to make your own and post by hand.

    python3 reddit/assist.py --thread thread.txt
    python3 reddit/assist.py --thread thread.txt --angle "I'd mention losing touch with my college roommate"
    python3 reddit/assist.py --thread thread.txt --sub AskWomenOver30
    pbpaste | python3 reddit/assist.py --angle "keep it short, just be helpful"

It applies the genuineness rules: help first (presence, not promotion), no
statistics or numbers, no em dash, casual and human, disclose you are the builder
only if your angle asks to mention Clarence, and casual (never clinical) framing
for health topics. It flags AI tells and reminds you to make the words yours
before you post.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[1]
LOCAL_AEO = Path.home() / "Library" / "Application Support" / "clarence-aeo"

# Optional grounding: the founder's own voice bank / mined ICP language, if present.
# All local and gitignored, so absent in a fresh checkout. Degrades gracefully.
CORPUS = next((p for p in (
    REPO / "content" / "grounding.jsonl",
    REPO / "scratch" / "reddit" / "icp_corpus.jsonl",
) if p.exists()), None)


def load_env() -> None:
    for path in (LOCAL_AEO / ".env", REPO / "scratch" / "aeo" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def grounding() -> str:
    """A little of the founder's real language, so drafts sound like a person."""
    if not CORPUS:
        return ""
    quotes = []
    for line in CORPUS.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        quotes += r.get("verbatim_quotes", [])
    quotes = list(dict.fromkeys(quotes))[:8]
    return ("REAL LANGUAGE people use for this (echo the plain, human register, do "
            "not quote verbatim): " + " | ".join(f'"{q}"' for q in quotes)) if quotes else ""


# ------------------------------------------------------------------ the draft
SYSTEM = (
    "You help a founder write ONE genuine, helpful Reddit reply in his own casual "
    "voice. He will edit it and post it himself, by hand. This is a real person in "
    "a community, not marketing. HARD RULES: "
    "(1) Be genuinely helpful and human first. Presence, not promotion. Answer the "
    "actual person in the thread. "
    "(2) Never use statistics, numbers, or cited data. Speak from lived experience "
    "and plain observation. A stat in a comment reads as marketing and gets called out. "
    "(3) Never use an em dash. Use a period, comma, colon, or parentheses. "
    "(4) Casual, plain, a little imperfect, the way people actually talk on Reddit. "
    "Short. No corporate warmth, no 'Great question', no lists unless it truly fits. "
    "(5) Do NOT mention Clarence or any product UNLESS the founder's angle explicitly "
    "asks. If it does, disclose plainly ('full disclosure, I built a small thing for "
    "this') and keep it a mention, never a pitch. "
    "(6) For health or mental-health topics (ADHD, anxiety, burnout, etc.): never "
    "diagnose, never give medical advice, never assume the person has the condition. "
    "Frame it as a casual feeling ('that ADHD-brain thing'), not a clinical fact. "
    "(7) Match the thread's tone. Return ONLY the reply text, no preamble, no quotes."
)


def draft_reply(thread: str, angle: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY not set (env or the AEO .env)")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    parts = [f"The Reddit thread you are replying to:\n\n{thread.strip()}"]
    if angle.strip():
        parts.append(f"The founder's angle for this reply (use it, in his voice):\n{angle.strip()}")
    g = grounding()
    if g:
        parts.append(g)
    parts.append("Write the reply now. Only the reply text.")
    payload = {"model": model, "max_tokens": 700, "system": SYSTEM,
               "messages": [{"role": "user", "content": "\n\n".join(parts)}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.load(resp)
    return "".join(b.get("text", "") for b in data.get("content", [])).strip()


def no_emdash(s: str) -> str:
    # Written as escapes, not the character, so scripts/no-emdash.sh can police
    # this file without flagging the very line that enforces the rule. Same
    # reason as enrich.py. See CLAUDE.md "Writing rules".
    return s.replace(" \u2014 ", ". ").replace("\u2014", ", ")


# ------------------------------------------------------------------ the checks
STAT_RE = [
    re.compile(r"\b\d+(?:\.\d+)?\s*%"),
    re.compile(r"\b\d+(?:\.\d+)?\s+percent\b", re.I),
    re.compile(r"\b\d+\s+(?:in|out of)\s+\d+\b", re.I),
    re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten)\s+"
               r"(?:in|out of)\s+(?:two|three|four|five|six|seven|eight|nine|ten|hundred|\d+)\b", re.I),
    re.compile(r"\b(?:studies|research|surveys?|polls?|statistics|data)\s+"
               r"(?:show|shows|found|find|suggest|suggests|say|says|indicate|indicates)\b", re.I),
    re.compile(r"\baccording to (?:a |an |the |one )?(?:study|survey|poll|report|research)\b", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s+(?:million|billion|thousand)\b", re.I),
]

# AI tells, a backstop under the human voice, not a substitute for it.
TELL_RE = [
    (re.compile(r"\bit'?s not (just |only )?\w+[, ]+(it'?s|but)\b", re.I), "the 'not just X, it's Y' construction"),
    (re.compile(r"\b(certainly|great question|absolutely!|i hope this helps)\b", re.I), "assistant-speak"),
    (re.compile(r"\b(delve|tapestry|testament|underscore|navigate the|resonate)\b", re.I), "AI-vocabulary tell"),
    (re.compile(r"\bin today'?s (fast-paced |digital )?world\b", re.I), "filler opener"),
    (re.compile(r"[\U0001F300-\U0001FAFF☀-➿]"), "emoji (fine sparingly, but check it's your style)"),
]


def stat_gate(text: str) -> list[str]:
    hits = []
    for rx in STAT_RE:
        for m in rx.finditer(text or ""):
            hits.append(m.group(0).strip())
    return list(dict.fromkeys(hits))


def tell_check(text: str) -> list[str]:
    found = []
    for rx, label in TELL_RE:
        if rx.search(text or ""):
            found.append(label)
    return list(dict.fromkeys(found))


# ------------------------------------------------------------------ main
def read_thread(path: str | None) -> str:
    if path:
        return Path(path).read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Paste a thread via --thread <file> or pipe it on stdin.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Draft a genuine Reddit reply you edit and post by hand.")
    ap.add_argument("--thread", help="path to a text file with the pasted post + comments")
    ap.add_argument("--angle", default="", help="your own angle/thoughts for the reply")
    ap.add_argument("--sub", default="", help="subreddit name, for the pre-post checklist")
    args = ap.parse_args()

    load_env()
    thread = read_thread(args.thread)
    if not thread.strip():
        raise SystemExit("empty thread.")

    reply = no_emdash(draft_reply(thread, args.angle))
    stats = stat_gate(reply)
    tells = tell_check(reply)

    bar = "=" * 60
    print(f"\n{bar}\nSUGGESTED REPLY (a draft, make it yours before posting)\n{bar}\n")
    print(reply)
    print(f"\n{bar}\nCHECKS\n{bar}")
    print(f"  statistics:  {'FOUND ' + str(stats) + ' -> cut them, no data in posts' if stats else 'clean, no numbers'}")
    print(f"  AI tells:    {', '.join(tells) if tells else 'none obvious'}")
    sub = f" in r/{args.sub}" if args.sub else ""
    print(f"\n{bar}\nBEFORE YOU POST{sub}\n{bar}")
    print("  [ ] Rewrite it in your own words. A real edit, not a rubber stamp. It has to sound like you.")
    print("  [ ] Confirm this subreddit lets you comment (flair, karma, account age, self-promo rules).")
    print("  [ ] No numbers, no pitch. Disclose you built it only if you chose to mention Clarence.")
    print("  [ ] Post from your own browser, on your own time. This tool never posts.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
