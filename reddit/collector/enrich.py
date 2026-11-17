#!/usr/bin/env python3
"""Reddit intelligence: read one thread, mine the emotion, draft a reply.

For a thread the founder is reviewing in the cockpit, this:
  1. scrapes the post + comments (old.reddit.com HTML, no OAuth app needed),
  2. asks Claude for an emotional read (pain points, the exact words people use,
     what they actually want) + a vibe-matched reply draft in Clarence's voice,
  3. appends the mined signals to a local ICP corpus (icp_corpus.jsonl) so the
     qualitative patterns accumulate for persona / positioning / site copy.

ON-DEMAND ONLY by design: one thread per click while you review, polite UA,
cached. It never posts. You edit the draft and post it yourself.

CLI:  python3 enrich.py "<reddit-thread-url>"
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store  # noqa: E402

# Code lives here, in version control. Data and secrets do not, and must not:
# reddit_creds.json sits in the data dir, and the corpus below is a file of real
# people's words. store._data_dir works out where that is from its own position,
# and this file reuses it rather than keeping a second answer that could drift.
DATA = store.DATA
CORPUS = DATA / "icp_corpus.jsonl"
# The Anthropic key lives with the AEO runtime (moved out of the repo 2026-07-14).
AEO = Path.home() / "Library" / "Application Support" / "clarence-aeo"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125 Safari/537.36")
MD_RE = re.compile(r'<div class="md">(.*?)</div>', re.S)
TAG_RE = re.compile(r"<[^>]+>")


# ----------------------------------------------------------------- env / keys
def load_env() -> None:
    for path in (AEO / ".env", DATA.parent / "aeo" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v


# ---------------------------------------------------------------- scrape
# The thread feed ends every entry with Reddit's own furniture. Left in, it
# becomes part of the post as far as the drafter is concerned.
FEED_CHROME = re.compile(
    r"\s*submitted by\s*/u/\S+(?:\s*to\s*/?r/\S+)?\s*\[link\]\s*\[comments\]\s*$",
    re.I | re.S)


def _text(block: str) -> str:
    out = html.unescape(TAG_RE.sub(" ", block)).strip()
    out = FEED_CHROME.sub("", out).strip()
    return re.sub(r"[ \t]{2,}", " ", out)


# Subreddit bots post the same wall of rules under every thread. As context it
# is worse than nothing: it is the longest "comment" in most threads, so it
# crowds out the real ones and the drafter treats it as what people said.
BOTS = ("automoderator", "[deleted]", "[removed]", "remindmebot",
        "sneakpeekbot", "wikisummarizerbot", "savevideo", "repostsleuthbot")


def is_bot(author: str) -> bool:
    a = (author or "").strip().lower().lstrip("/").removeprefix("u/")
    return a in BOTS or a.endswith("bot")


class LoginWalled(RuntimeError):
    """Reddit answered with its login page instead of the thread.

    Raised rather than returned on purpose. The old behaviour was to hand the
    login page back as if it were the post, so a draft got written about
    "Welcome to Reddit" and nothing anywhere said so. A loud failure is worth
    more than a confident wrong answer.
    """


class RateLimited(RuntimeError):
    """Reddit is throttling. The thread feed allows about one read at a time,
    which is fine for reviewing one post at a time and not for a batch."""


def fetch_oauth_thread(url: str, max_comments: int = 40) -> dict | None:
    """The post and its comments over the Reddit API, when credentials exist.

    Returns None when they do not, which today is always: the credentials are
    not available and are not coming (see CLAUDE.md), so every call falls through
    to the feed. Kept because the fall-through is the whole design and because it
    costs one dict lookup, not because it is pending.
    """
    try:
        creds = json.loads((DATA / "reddit_creds.json").read_text())
    except Exception:
        return None
    if not (creds.get("client_id") or "").strip():
        return None
    import monitor as _mon
    token = _mon.get_token(creds, UA)
    if not token:
        return None
    m = re.search(r"/comments/([a-z0-9]+)", url)
    if not m:
        return None
    data = _mon.get_json(
        f"https://oauth.reddit.com/comments/{m.group(1)}"
        f"?limit={max_comments}&raw_json=1&depth=2", UA, token)
    if not isinstance(data, list) or len(data) < 1:
        return None
    post = (data[0].get("data", {}).get("children") or [{}])[0].get("data", {})
    comments = []
    for c in (data[1].get("data", {}).get("children") if len(data) > 1 else []) or []:
        d = c.get("data", {})
        body = (d.get("body") or "").strip()
        if len(body) > 15 and not is_bot(d.get("author")):
            who = d.get("author") or ""
            comments.append(f"/u/{who}: {body}" if who else body)
    return {"url": url, "title": post.get("title", ""),
            "selftext": (post.get("selftext") or "").strip(),
            "comments": comments[:max_comments], "n_comments": len(comments),
            "via": "oauth"}


def fetch_rss_thread(url: str, max_comments: int = 40) -> dict:
    """The post and its comments from the thread's own Atom feed.

    Why this and not the HTML or the .json: as of October 2026 logged-out reads
    of both return a login wall or an app shell, while `<thread>.rss` still
    answers with the full selftext. It is rate limited to roughly one request
    at a time, so it suits a reviewer working through one post at a time and
    must never be used to sweep a queue.
    """
    import xml.etree.ElementTree as ET

    feed = url.rstrip("/") + "/.rss"
    req = urllib.request.Request(feed, headers={
        "User-Agent": UA, "Accept": "application/atom+xml, application/xml"})
    # This feed throttles after roughly one read, and the throttle clears in a
    # couple of seconds. Waiting it out here is worth it: the alternative the
    # reader sees is "go and open Reddit yourself", which is the whole thing
    # this is meant to remove. Still one THREAD per call, just patient about it.
    raw = None
    for attempt, pause in enumerate((1.5, 4.0, 8.0)):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            break
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            if attempt == 2:
                raise RateLimited(
                    "Reddit kept throttling the thread feed. Give it a minute, "
                    "or paste the post in.") from e
            time.sleep(pause)
    if raw is None:
        raise RateLimited("Reddit is throttling the thread feed.")
    if "welcome to reddit" in raw[:3000].lower():
        raise LoginWalled("Reddit returned its login page for that thread.")

    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    title, selftext, comments = "", "", []
    for entry in root.findall("a:entry", ns):
        eid = (entry.findtext("a:id", default="", namespaces=ns) or "")
        content = entry.findtext("a:content", default="", namespaces=ns) or ""
        body = _text(content)
        # t3_ is the post itself, t1_ is a comment. Keyed on the id rather than
        # on position, because the feed does not promise the post comes first.
        if eid.startswith("t3_"):
            title = _text(entry.findtext("a:title", default="", namespaces=ns) or "")
            selftext = body
        elif eid.startswith("t1_") and len(body) > 15:
            who = (entry.findtext("a:author/a:name", default="", namespaces=ns) or "").strip()
            if is_bot(who):
                continue
            comments.append(f"{who}: {body}" if who else body)
    if not selftext and not comments:
        raise LoginWalled("That feed had no post and no comments in it.")
    return {"url": url, "title": title, "selftext": selftext,
            "comments": comments[:max_comments], "n_comments": len(comments),
            "via": "rss"}


def scrape_thread(url: str, max_comments: int = 40) -> dict:
    """Post title + selftext + top comments.

    Tries the thread's Atom feed first, which is the only logged-out path that
    still returns the real post, then falls back to the old.reddit HTML that
    used to work. A login wall from BOTH is raised, never returned: drafting
    against Reddit's login page is a silent wrong answer.
    """
    try:
        got = fetch_oauth_thread(url, max_comments)
        if got and (got.get("selftext") or got.get("comments")):
            return got
    except Exception:
        pass
    try:
        return fetch_rss_thread(url, max_comments)
    except RateLimited:
        raise
    except Exception:
        pass
    old = url.replace("://www.", "://old.").replace("://reddit.", "://old.reddit.")
    if "old.reddit.com" not in old:
        old = old.replace("://reddit.com", "://old.reddit.com")
    req = urllib.request.Request(old, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        page = resp.read().decode("utf-8", errors="ignore")
    if "welcome to reddit" in page[:4000].lower() or "/login/" in resp.url:
        raise LoginWalled(
            "Reddit login-walled this thread and its feed did not answer. "
            "Paste the post in instead, or set the API credentials.")
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    title = _text(m.group(1)) if m else ""
    blocks = [_text(b) for b in MD_RE.findall(page)]
    blocks = [b for b in blocks if len(b) > 15]
    selftext = blocks[0] if blocks else ""
    comments = blocks[1:max_comments + 1]
    return {"url": url, "title": title, "selftext": selftext,
            "comments": comments, "n_comments": len(comments), "via": "html"}


def scrape_people(url: str, max_people: int = 60) -> list[dict]:
    """Who is IN this thread, and what each of them said.

    The poster is one person with the problem. The comments are often five more,
    volunteering that they have it too: "I'm the one who always reaches out",
    "my best friend is a terrible texter". Those are the same audience and nobody
    collects them, because scrapers read posts and ignore who replied.
    """
    old = url.replace("://www.", "://old.").replace("://reddit.", "://old.reddit.")
    if "old.reddit.com" not in old:
        old = old.replace("://reddit.com", "://old.reddit.com")
    req = urllib.request.Request(old, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        page = resp.read().decode("utf-8", errors="ignore")

    out, seen = [], set()
    for block in page.split('<div class=" thing ')[1:]:
        author = re.search(r'data-author="([^"]+)"', block)
        if not author:
            continue
        who = author.group(1)
        if who in ("[deleted]", "AutoModerator") or who in seen:
            continue
        fullname = re.search(r'data-fullname="(t[13]_[a-z0-9]+)"', block)
        kind = "op" if (fullname and fullname.group(1).startswith("t3_")) else "commenter"
        md = re.search(r'<div class="md">(.*?)</div>', block, re.S)
        text = _text(md.group(1)) if md else ""
        if len(text) < 25:                 # one-liners say nothing about a person
            continue
        perma = re.search(r'data-permalink="([^"]+)"', block)
        seen.add(who)
        out.append({"author": who, "kind": kind, "text": text[:1200],
                    "url": ("https://www.reddit.com" + perma.group(1)) if perma else url})
        if len(out) >= max_people:
            break
    return out


# ---------------------------------------------------------------- analyze
SYSTEM = (
    "You are Clarence's founder-side research analyst. Clarence is a private "
    "Mac app that helps people keep up with the friends and family who matter, "
    "drafting messages in the user's own voice from their iMessage history. "
    "Be exact about privacy: the messages stay on the Mac, and only a "
    "de-identified excerpt, with names and numbers stripped, is sent out to "
    "write a draft. Never claim that nothing leaves the Mac, because that is "
    "no longer true. You read one Reddit thread and return "
    "STRICT JSON only, no prose around it. Voice rules for any draft: warm, "
    "human, first-person, presence not promotion, honest. Never hard-pitch, "
    "never drop a link unless it genuinely answers the question, never use an "
    "em dash. If Clarence is not actually a fit for the thread, say so and set "
    "suggested_reply to a helpful non-promotional comment instead."
)

SCHEMA = """Return JSON with exactly these keys:
{
  "emotional_themes": [short strings, the feelings driving the thread],
  "pain_points": [short strings, concrete problems people describe],
  "verbatim_quotes": [1-5 short exact phrases people used, for ICP research],
  "what_they_want": [short strings, the job-to-be-done they're asking for],
  "clarence_fit": "none" | "weak" | "strong",
  "fit_reason": one sentence,
  "suggested_reply": a ready-to-edit comment in the founder's own voice
}"""


def thread_from_text(text: str, url: str = "") -> dict:
    """A thread built from what you pasted, not from the network.

    Paste is better than scraping whenever you have the tab open: you get the
    whole post instead of a search excerpt, it works on threads the scraper
    cannot reach, and you can trim it to the part you actually want answered.
    First line is treated as the title if it looks like one.
    """
    text = (text or "").strip()
    lines = text.splitlines()
    title, body = "", text
    first = lines[0].strip() if lines else ""
    looks_like_a_title = first and len(first) < 160 and (
        first.endswith("?") or not first.endswith((".", "!")))
    if looks_like_a_title:
        title, body = first, "\n".join(lines[1:]).strip()
    return {"url": url, "title": title, "selftext": body or text,
            "comments": [], "n_comments": 0, "pasted": True}


def analyze(thread: dict, model: str | None = None) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set (checked the AEO .env)")
    model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    convo = f"THREAD TITLE: {thread['title']}\n\nORIGINAL POST:\n{thread['selftext']}\n\n"
    if thread["comments"]:
        convo += "COMMENTS:\n" + "\n---\n".join(thread["comments"][:25])
    payload = {
        "model": model, "max_tokens": 1600, "system": SYSTEM,
        "messages": [{"role": "user",
                      "content": f"{SCHEMA}\n\nThread to analyze:\n\n{convo}"}],
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Anthropic HTTP {e.code}: {e.read().decode()[:200]}")
    text = "".join(b.get("text", "") for b in data.get("content", []))
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError("model did not return JSON:\n" + text[:400])


def append_corpus(thread: dict, analysis: dict, when: str | None = None) -> None:
    """One line per analyzed thread: the accumulating ICP research corpus."""
    row = {
        "when": when, "url": thread["url"], "title": thread["title"],
        "clarence_fit": analysis.get("clarence_fit"),
        "emotional_themes": analysis.get("emotional_themes", []),
        "pain_points": analysis.get("pain_points", []),
        "verbatim_quotes": analysis.get("verbatim_quotes", []),
        "what_they_want": analysis.get("what_they_want", []),
    }
    with CORPUS.open("a") as f:
        f.write(json.dumps(row) + "\n")


def enrich(url: str, when: str | None = None, post_text: str | None = None) -> dict:
    load_env()
    thread = thread_from_text(post_text, url) if (post_text or "").strip() else scrape_thread(url)
    analysis = analyze(thread)
    append_corpus(thread, analysis, when)
    return {"thread": {k: thread[k] for k in ("url", "title", "n_comments")},
            "analysis": analysis}


# ---------------------------------------------------------------- draft a reply
PERSONAS = DATA.parent / "content" / "personas.json"


def _anthropic(system: str, user: str, max_tokens: int = 900) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set (checked the AEO .env)")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    payload = {"model": model, "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Anthropic HTTP {e.code}: {e.read().decode()[:200]}")
    return "".join(b.get("text", "") for b in data.get("content", []))


def load_author(author_id: str | None) -> dict | None:
    if not PERSONAS.exists():
        return None
    authors = json.loads(PERSONAS.read_text()).get("authors", [])
    if not authors:
        return None
    return next((a for a in authors if a["id"] == author_id), authors[0])


def _author_system(author: dict | None) -> str:
    if not author:
        return ("Write a warm, honest, first-person Reddit comment. Plain, human, "
                "not salesy. Never use an em dash.")
    v = author.get("voice", {})
    det = author.get("your_details", {})
    real = [f"{k}: {val}" for k, val in det.items() if k != "_note" and val]
    s = (f"You are writing a Reddit comment AS this person, in their own voice.\n"
         f"WHO: {author.get('bio', '')}\n"
         f"WHY IT MATTERS TO THEM: {' '.join(author.get('why', []))}\n"
         f"VOICE, DO: {' '.join(v.get('do', []))}\n"
         f"VOICE, DO NOT: {' '.join(v.get('dont', []))}\n"
         f"THEY SOUND LIKE: {' | '.join(author.get('sounds_like', []))}\n")
    if real:
        s += ("REAL DETAILS (use naturally, invent nothing beyond these): "
              + "; ".join(real) + "\n")
    else:
        s += ("NO personal specifics were provided. Do NOT fabricate a specific "
              "anecdote, a named friend, a job, or a dated event. Speak to the "
              "feeling in general terms and leave a natural gap for a real story, "
              "so the founder can drop his own in. Honesty is the whole point.\n")
    s += ("Write like a real human in a thread, not a marketer. If the product (a "
          "private Mac app that drafts messages in your own voice, reading your "
          "iMessage history on your own machine) genuinely fits the moment, you may "
          "mention it ONCE and disclose you built it; otherwise do not mention it at "
          "all. If privacy comes up, the only honest line is that the messages stay "
          "on the Mac and a de-identified excerpt, names and numbers stripped, is "
          "sent out only to write the draft. NEVER write that nothing leaves the "
          "Mac, that nothing is uploaded, or anything else absolute about it.")
    # Everything above is a DESCRIPTION of a voice, which is the weaker way to
    # ask for one. Below, where there are enough of them, are replies he actually
    # sent: a model matches a voice from examples of it far better than from any
    # amount of prose about it. Empty until there are three, and it stays empty
    # if anything at all goes wrong reading them, because a draft that is a
    # little generic beats no draft.
    try:
        import voice
        con = store.connect()
        s += voice.example_block(con, author.get("id"))
        con.close()
    except Exception:
        pass
    s += "\nNever use an em dash."
    return s


CONTEXTS = {
    "new_comment": "Write a NEW top-level comment responding to the original post.",
    "reply_comment": "REPLY to the one specific comment shown below, in context.",
    "reply_to_reply": ("REPLY to the person who answered YOUR comment. They are "
                       "already in conversation with you, so there is nothing to "
                       "establish: no introduction, no restating your original "
                       "point, no mention of what you are building unless they "
                       "asked. Answer what they actually said and nothing else."),
    "seed_question": ("Write an ORIGINAL post to genuinely ask the community. Give a "
                      "one-line 'Title: ...' then the body. Ask a real question, do "
                      "not pitch anything."),
}


# How long the thing should be, per context. A first comment has to earn its
# place in a thread; a reply to someone who already answered you does not, and
# padding it is the fastest way to sound like a bot.
LENGTHS = {
    "default": ("LENGTH: keep it short, the length of a real Reddit comment. "
                "Two to four sentences, one idea. Do NOT write an essay or stack up "
                "paragraphs of advice. Do NOT play therapist, diagnose the person, or "
                "tell them to seek professional help unless they explicitly asked. "
                "Relate to them first, then at most one small suggestion or a real "
                "question. Match the energy and length of the thread."),
    "reply_to_reply": ("LENGTH: very short. One to three sentences. This is the "
                       "second turn of a conversation that is already going, so it "
                       "should read like a person typing back on their phone. No "
                       "summary of the thread, no fresh advice, no sign-off. If a "
                       "single sentence answers them, send a single sentence."),
}


def draft_reply(url: str, author_id: str = "me-founder",
                context: str = "new_comment", target_comment: str | None = None,
                thread: dict | None = None, angle: str | None = None,
                post_text: str | None = None, my_comment: str | None = None) -> dict:
    """Draft one reply. `angle` is what YOU want to say, in your own rough words
    (dictated is ideal): it is the spine of the draft, not a hint. `post_text`
    is the post pasted straight from the tab you have open, which beats anything
    a scraper can see."""
    load_env()
    if thread is None:
        thread = (thread_from_text(post_text, url) if (post_text or "").strip()
                  else scrape_thread(url))
    system = _author_system(load_author(author_id))
    user = (f"THREAD TITLE: {thread['title']}\n\n"
            f"ORIGINAL POST:\n{thread.get('selftext', '')}\n\n")
    if context in ("reply_comment", "reply_to_reply") and target_comment:
        if context == "reply_to_reply" and (my_comment or "").strip():
            user += ("WHAT YOU ALREADY SAID IN THIS THREAD, which they are "
                     f"answering:\n{my_comment.strip()}\n\n")
        user += f"THE COMMENT YOU ARE REPLYING TO:\n{target_comment}\n\n"
    elif thread.get("comments"):
        user += "SOME COMMENTS FOR CONTEXT:\n" + "\n---\n".join(thread["comments"][:8]) + "\n\n"
    angle = (angle or "").strip()
    if angle:
        user += (
            "WHAT HE WANTS TO SAY, in his own words. This was dictated, so it may "
            "be rough, repeat itself, or trail off. It is still the SPINE of the "
            "reply, not a suggestion:\n"
            f"{angle}\n\n"
            "Build the comment around HIS point. Keep his specifics, his examples "
            "and his opinion, even the blunt ones. Clean up the grammar and the "
            "false starts, keep his words wherever they already work, and do not "
            "smooth him into someone more polite than he is. Do not add advice he "
            "did not give. If what he says does not fully answer the post, that is "
            "fine, say his piece and stop.\n\n")
    user += (CONTEXTS.get(context, CONTEXTS["new_comment"])
             + "\n\n" + LENGTHS.get(context, LENGTHS["default"])
             + "\nWrite only the text, ready to paste. No preamble, no quotes around it.")
    cap = 160 if context == "reply_to_reply" else (320 if angle else 220)
    reply = _anthropic(system, user, cap).strip()
    # Never ship an em dash. Written as escapes, not the character, so
    # scripts/no-emdash.sh can police this file without flagging the very
    # line that enforces the rule. See CLAUDE.md "Writing rules".
    reply = reply.replace(" \u2014 ", ". ").replace("\u2014", ", ")
    return {"draft": reply, "comments": thread.get("comments", [])[:8],
            "author": author_id, "context": context}


# The things that are actually wrong with a first draft, most of the time. Each
# is an EDIT, not a fresh attempt: the point is to keep what you liked.
REVISIONS = {
    "shorter": "Cut it down hard. Same point, fewer words. Lose any sentence that "
               "is not doing work. If it can be two sentences, make it two.",
    "warmer": "Make it sound like a person who has been there, not someone being "
              "helpful at them. Less careful, more human. Contractions are fine.",
    "less-advice": "Stop advising. Keep the part where he relates to them, drop the "
                   "suggestions, or leave at most one. Ending without a fix is fine.",
    "more-me": "Plainer. Take out anything that reads as written rather than said: "
               "tidy parallel phrases, wrapped-up endings, any word he would not "
               "use out loud. Slightly uneven is better than polished.",
    "less-salesy": "Take the pitch out. He is a person in a thread who happens to "
                   "have built something, not someone selling it. If the product "
                   "does not need to be named, do not name it.",
    "no-question": "Drop the question at the end. Let it just be a thing he said.",
}


# Where people are sent. Until a survey page exists this is just the site; set
# CLARENCE_SURVEY_URL once there is somewhere specific to land.
SURVEY_URL = os.environ.get("CLARENCE_SURVEY_URL",
                            "https://getclarence.app/survey?utm_source=reddit"
                            "&utm_medium=ask&utm_campaign=first10")

ASK_RULES = """You are writing a short message asking ONE person, who has just
been talking with you in a Reddit thread, whether they would try the thing you
built and tell you what they think.

Hard rules, all of them non-negotiable:
- Say that YOU built it. Not "I found this", not "there's an app". He built it,
  and burying that is the single fastest way to be read as a shill.
- ONE link, at the end. Never two.
- Do not describe it as private, secure, or local. Whatever it does with data is
  not settled yet and a promise made here would be a promise he has to keep.
- No feature list. One sentence on what it does, at most.
- Ask for their reaction, not a signup. The point is to learn, not to convert.
- Short. Three sentences is plenty, two is better. This is a person replying on
  their phone, not a landing page.
- Make it specific to what THEY said. If they described losing touch with
  someone, say that back in their words. A message that would fit anybody reads
  as a template, because it is one.
- No hype words: revolutionary, game-changing, seamless, effortless.
- If asking would be tone deaf given what they wrote, say so instead of writing
  the message. Reply with exactly: SKIP: <one line on why>.
"""


def draft_ask(author: str, their_words: str, my_comment: str = "",
              channel: str = "thread", author_id: str = "me-founder") -> dict:
    """Draft the ask. `their_words` is what this person actually said, which is
    what keeps it from reading as a template."""
    load_env()
    system = _author_system(load_author(author_id))
    where = ("This is a REPLY IN THE THREAD, public, where the conversation "
             "already is." if channel == "thread" else
             "This is a DIRECT MESSAGE. They have been talking with him, so it "
             "continues a conversation rather than starting one. Be even "
             "shorter and more obviously human than a public reply would be.")
    user = (f"{ASK_RULES}\n{where}\n\n"
            f"WHAT THEY SAID:\n{(their_words or '').strip()[:1500]}\n\n")
    if (my_comment or "").strip():
        user += f"WHAT HE SAID TO THEM EARLIER:\n{my_comment.strip()[:800]}\n\n"
    user += (f"THE LINK: {SURVEY_URL}\n\n"
             "Write only the message, ready to send. No preamble, no quotes.")
    out = _anthropic(system, user, 220).strip()
    out = out.replace(" \u2014 ", ". ").replace("\u2014", ", ")
    if out.upper().startswith("SKIP"):
        return {"ok": False, "skip": True, "reason": out[5:].strip(" :")}
    return {"ok": True, "text": out, "channel": channel, "url": SURVEY_URL}


def revise_draft(draft: str, instruction: str, post_text: str | None = None,
                 angle: str | None = None, author_id: str = "me-founder") -> dict:
    """Change a draft: surgically for a preset button, from scratch for anything he says.

    These used to be the same request, and that was the bug. A preset button is a
    genuine surgical edit, and "keep everything else" is right for it: rewriting
    from scratch on every tweak is how you lose the version you nearly liked.

    Free text he types or dictates is not that. It is him saying what the draft
    gets WRONG. Sent down the surgical path it arrived wrapped in four separate
    orders to change nothing else, and the only way to satisfy both "say this as
    well" and "leave the rest alone" is to bolt a paragraph onto the end. So that
    is what it did, every time. Free text now re-drafts: the post, his original
    angle and what he just said, resolved into one reply rather than stacked.
    """
    load_env()
    preset = instruction in REVISIONS
    what = REVISIONS.get(instruction, instruction).strip()
    system = _author_system(load_author(author_id))
    user = ""
    if (post_text or "").strip():
        user += f"THE POST HE IS REPLYING TO:\n{post_text.strip()[:4000]}\n\n"
    if (angle or "").strip():
        user += ("WHAT HE WANTED TO SAY, in his own words:\n"
                 f"{angle.strip()}\n\n")
    user += f"THE CURRENT DRAFT:\n{draft.strip()}\n\n"
    if preset:
        user += (f"CHANGE THIS, and only this:\n{what}\n\n"
                 "Keep everything else as close to the current draft as you can. "
                 "This is an edit, not a new attempt: do not reach for a different "
                 "angle, a different opening or a different point. Keep his words "
                 "where they already work.\n")
    else:
        user += (
            "WHY HE IS REWRITING IT. This is him telling you what the draft gets "
            f"wrong, and it is the most important thing on this page:\n{what}\n\n"
            "Write the comment AGAIN. Do not edit the draft, and do not keep its "
            "shape. Go back to the post, to what he wanted to say, and to what he "
            "has just told you, and write ONE reply that holds all three at once.\n"
            "What he just said is the point of the comment now, so it belongs in "
            "the first sentence or two. If it replaces what the draft was arguing, "
            "then drop what the draft was arguing. NEVER answer this by keeping the "
            "draft and adding a sentence or a paragraph to the end: that is the "
            "exact thing he is complaining about.\n"
            "The draft below is a reference for tone and for any phrase worth "
            "keeping. It is not a thing to be preserved.\n")
    user += ("Still true: no statistics or numbers, no em dashes, no therapy voice, "
             "and the length of a real Reddit comment.\n"
             "Write only the revised text, ready to paste. No preamble, no notes on "
             "what you changed, no quotes around it.")
    out = _anthropic(system, user, 400).strip()
    out = out.replace(" \u2014 ", ". ").replace("\u2014", ", ")
    return {"draft": out, "applied": instruction, "rewrote": not preset}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    out = enrich(sys.argv[1])
    print(json.dumps(out, indent=2))
