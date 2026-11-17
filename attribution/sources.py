"""Source classification, kept identical to the site's live classifier.

The site buckets every signup's first-touch origin in
site/functions/api/waitlist.js (classifySource). The attribution engine has to
bucket TRAFFIC the same way, or the join is apples to oranges: a session from
chatgpt.com and a signup from chatgpt.com must land in the same "ai:chatgpt"
row. So these pattern tables are a deliberate copy of that file. If you edit
one, edit both. There is a parity test at the bottom of this module's README.

Returns a short bucket like "ai:chatgpt", "search:google", "social:reddit",
"utm:<source>", "other:<host>", or "direct".
"""

from urllib.parse import urlparse

AI_PATTERNS = [
    ("chatgpt.com", "chatgpt"),
    ("chat.openai.com", "chatgpt"),
    ("oai.", "chatgpt"),
    ("perplexity.ai", "perplexity"),
    ("gemini.google.", "gemini"),
    ("bard.google.", "gemini"),
    ("claude.ai", "claude"),
    ("copilot.microsoft.com", "copilot"),
    ("bing.com/chat", "copilot"),
    ("edgeservices.bing.com", "copilot"),
    ("you.com", "you"),
    ("poe.com", "poe"),
    ("phind.com", "phind"),
]
SEARCH_PATTERNS = [
    ("google.", "google"),
    ("bing.com", "bing"),
    ("duckduckgo.com", "duckduckgo"),
    ("search.brave.com", "brave"),
    ("ecosia.org", "ecosia"),
    ("search.yahoo.com", "yahoo"),
]
SOCIAL_PATTERNS = [
    ("reddit.com", "reddit"),
    ("x.com", "x"),
    ("twitter.com", "x"),
    ("t.co", "x"),
    ("news.ycombinator.com", "hackernews"),
    ("producthunt.com", "producthunt"),
    ("facebook.com", "facebook"),
    ("instagram.com", "instagram"),
    ("linkedin.com", "linkedin"),
    ("lnkd.in", "linkedin"),
    ("youtube.com", "youtube"),
]


def host_of(url: str) -> str:
    try:
        h = urlparse(url).hostname or ""
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def classify(referrer: str = "", utm_source: str = "") -> str:
    ref = (referrer or "").lower()
    for pat, name in AI_PATTERNS:
        if pat in ref:
            return f"ai:{name}"
    for pat, name in SEARCH_PATTERNS:
        if pat in ref:
            return f"search:{name}"
    for pat, name in SOCIAL_PATTERNS:
        if pat in ref:
            return f"social:{name}"
    if utm_source:
        return f"utm:{utm_source.lower()[:40]}"
    host = host_of(ref)
    if host:
        return f"other:{host}"
    return "direct"


# A utm_source tag is the strongest signal we control. When an ad or a content
# link carries an explicit source, prefer it over the referrer host so paid and
# owned channels never get miscounted as "direct" or "other". Traffic rows that
# arrive from an analytics API already have a source dimension, so they use this.
def classify_from_source(utm_source: str = "", referrer: str = "") -> str:
    us = (utm_source or "").strip().lower()
    KNOWN = {
        "chatgpt": "ai:chatgpt",
        "perplexity": "ai:perplexity",
        "gemini": "ai:gemini",
        "claude": "ai:claude",
        "copilot": "ai:copilot",
        "google": "search:google",
        "bing": "search:bing",
        "reddit": "social:reddit",
        "x": "social:x",
        "twitter": "social:x",
        "hackernews": "social:hackernews",
        "producthunt": "social:producthunt",
        "meta": "paid:meta",
        "facebook": "paid:meta",
        "instagram": "paid:meta",
        "google-ads": "paid:google",
        "googleads": "paid:google",
        "newsletter": "owned:email",
        "email": "owned:email",
    }
    if us in KNOWN:
        return KNOWN[us]
    if us:
        return f"utm:{us[:40]}"
    return classify(referrer, "")
