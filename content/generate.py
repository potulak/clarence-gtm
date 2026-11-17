#!/usr/bin/env python3
"""Auto-draft one short /answers page that competes for a non-branded query.

Productionized for cloud execution (GitHub Actions). This lives in a TRACKED
directory (unlike scratch/), reads the committed AEO ledger, and carries the
structural safety the product panel required: a statistics hard-stop, so a
fabricated number can never quietly ship. The founder's merge is still the gate;
this never merges.

Pipeline per run:
  1. pick the next unwritten non-branded target we were absent from (AEO ledger),
  2. draft a short page with Claude, grounded in the ICP corpus if present,
  3. run the stat hard-stop (any statistic-shaped text routes the page to the
     founder for a number check) and the em-dash gate,
  4. write it into site/src/content/answers/<slug>.md (aiGenerated: true),
  5. with --pr: open a one-tap-merge PR via an isolated git worktree, labeled
     clean or NEEDS-STAT-CHECK.

    python3 generate.py --dry            # draft + print + gate, write nothing
    python3 generate.py                  # write the page + run gates (no push)
    python3 generate.py --query "..."    # force a specific target
    python3 generate.py --pr             # write, gate, open a PR off main

The published page is gated by the founder's merge. This never merges.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]          # .../clarence  (content/ is top-level)
HERE = Path(__file__).resolve().parent
ANSWERS_DIR = REPO / "site" / "src" / "content" / "answers"
STATE = HERE / "written.json"
VOICES = HERE / "voices.json"
PERSONAS = HERE / "personas.json"
LOCAL_AEO = Path.home() / "Library" / "Application Support" / "clarence-aeo"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _first_existing(paths):
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None


# The ledger: env override, then the committed repo ledger (what Actions reads),
# then the local runtime copy (what a laptop run reads).
LEDGER = _first_existing([
    os.environ.get("AEO_LEDGER"),
    REPO / "aeo" / "results.csv",
    LOCAL_AEO / "results.csv",
])
# Grounding corpus and pinned language are local-only (scratch/ is gitignored);
# absent in the cloud, and the pipeline degrades gracefully without them.
CORPUS = _first_existing([
    REPO / "content" / "grounding.jsonl",
    REPO / "scratch" / "reddit" / "icp_corpus.jsonl",
])
SAVED = _first_existing([LOCAL_AEO / "saved.json"])


# --------------------------------------------------------------- env / model
def load_env() -> None:
    for path in (LOCAL_AEO / ".env", REPO / "scratch" / "aeo" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


# --------------------------------------------------------------- pick target
def _branded(q: str) -> bool:
    return "clarence" in (q or "").lower()


# Topical-fit gate. The research is clear that topical drift (publishing outside
# your lane) is a top deindexing trigger. Our lane is relationships, keeping in
# touch, messaging, and the private-app angle. A query must touch one of these to
# be auto-drafted. A forced --query bypasses this (the founder chose it on purpose).
TOPIC_TERMS = (
    "text", "message", "imessage", "sms", "reply", "replies", "chat", "dm",
    "friend", "friends", "family", "relationship", "relationships",
    "reconnect", "reconnecting", "keep in touch", "keeping in touch",
    "stay in touch", "staying in touch", "reach out", "reaching out", "reach back",
    "drift", "drifted", "lost touch", "losing touch", "out of touch",
    "lonely", "loneliness", "distance", "long distance", "apart",
    "contact", "contacts", "check in", "checking in", "catch up", "catching up",
    "conversation", "conversations", "voice", "draft", "iphone", "mac",
    "macbook", "app", "privacy", "remember to", "forget to",
)


def on_topic(q: str) -> bool:
    ql = (q or "").lower()
    return any(t in ql for t in TOPIC_TERMS)


def written_slugs() -> set[str]:
    have = {p.stem for p in ANSWERS_DIR.glob("*.md")} if ANSWERS_DIR.exists() else set()
    if STATE.exists():
        try:
            have |= set(json.loads(STATE.read_text()).get("queries", []))
        except Exception:
            pass
    return have


def absent_queries() -> list[str]:
    """Non-branded questions we were absent from in the latest ledger run.

    Non-branded is the point: surfacing on a query that names Clarence is
    expected (the model scraped our site). The prize is ranking for a question
    that never mentions us."""
    if not LEDGER or not LEDGER.exists():
        return []
    rows = [r for r in csv.DictReader(LEDGER.open(newline=""))
            if DATE_RE.match((r.get("run_date") or "").strip())]
    if not rows:
        return []
    latest = sorted({r["run_date"] for r in rows})[-1]
    lr = [r for r in rows if r["run_date"] == latest]
    all_q = {r["query"] for r in lr if not _branded(r["query"])}
    hit = {r["query"] for r in lr if r.get("mentioned") == "1"}
    return sorted(all_q - hit)


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:70]


def pick_target(force: str | None) -> str | None:
    if force:
        return force
    done = written_slugs()
    skipped_off_topic = 0
    for q in absent_queries():
        if slugify(q) in done or q in done:
            continue
        if not on_topic(q):
            skipped_off_topic += 1
            continue
        return q
    if skipped_off_topic:
        print(f"(skipped {skipped_off_topic} off-lane queries via the topical-fit gate)")
    return None


# --------------------------------------------------------------- grounding
def grounding() -> str:
    """Real pains / wants / phrases from the mined ICP corpus, deduped."""
    if not CORPUS or not CORPUS.exists():
        return ""
    pains, wants, quotes = [], [], []
    for line in CORPUS.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        pains += r.get("pain_points", [])
        wants += r.get("what_they_want", [])
        quotes += r.get("verbatim_quotes", [])
    dedup = lambda xs: list(dict.fromkeys(xs))
    out = []
    if pains:
        out.append("PAINS people describe: " + "; ".join(dedup(pains)[:10]))
    if wants:
        out.append("WHAT THEY WANT: " + "; ".join(dedup(wants)[:8]))
    if quotes:
        out.append("THEIR EXACT WORDS: " + " | ".join(f'"{q}"' for q in dedup(quotes)[:8]))
    return "\n".join(out)


# --------------------------------------------------------------- voice / persona
def load_voice(vid: str | None):
    data = json.loads(VOICES.read_text())
    voices = data["voices"]
    v = next((x for x in voices if x["id"] == vid), voices[0])
    do = "; ".join(v.get("do", []))
    dont = "; ".join(v.get("dont", []))
    return v["id"], (f"VOICE = {v['name']}. {v['description']}\n"
                     f"  DO: {do}\n  DO NOT: {dont}")


def pick_persona(pid: str | None, query: str):
    if not PERSONAS.exists():
        return None
    ps = json.loads(PERSONAS.read_text())["personas"]
    if pid:
        return next((x for x in ps if x["id"] == pid), None)
    ql = set(re.findall(r"[a-z]{4,}", query.lower()))       # naive keyword match
    best, best_n = None, 0
    for p in ps:
        hay = " ".join([p["tagline"], p["who"]] + p.get("pains", []) +
                       p.get("wants", [])).lower()
        n = sum(1 for w in ql if w in hay)
        if n > best_n:
            best, best_n = p, n
    return best


def persona_block(p) -> str:
    if not p:
        return ""
    return (f"AUDIENCE = {p['name']}: {p['tagline']} {p['who']}\n"
            f"  Their pains: {'; '.join(p.get('pains', []))}\n"
            f"  What they want: {'; '.join(p.get('wants', []))}")


def pinned_language() -> str:
    if not SAVED or not SAVED.exists():
        return ""
    saved = json.loads(SAVED.read_text())
    notes = [v.get("note", "") for v in saved.values()
             if v.get("saved") and v.get("note")]
    return ("LANGUAGE TO ECHO (from saved sources): " + " | ".join(notes[:3])
            if notes else "")


# --------------------------------------------------------------- draft
SYSTEM = (
    "You write short, genuinely useful web answers for Clarence, a private "
    "on-device Mac app that helps people keep up with friends and family by "
    "drafting messages in their own voice from their iMessage history, nothing "
    "uploaded. You are writing ONE page that answers a real question people ask. "
    "HARD RULES: (1) Be actually helpful first, this is not a sales page. "
    "(2) Never invent statistics, study results, or numbers of any kind, omit "
    "them entirely. (3) Never use an em dash; use a period, comma, colon, or "
    "parentheses. (4) Warm, plain, human, honest. (5) Mention Clarence at most "
    "once, near the end, only if it genuinely fits, never a hard pitch. "
    "(6) Ground the answer in the real language people use, provided below. "
    "(7) For any health, mental-health, or medical topic (ADHD, anxiety, "
    "burnout, and the like): never give medical advice, never diagnose, and "
    "never assume or state that the reader has the condition. Use the casual, "
    "everyday way people talk about it as a FEELING, not a clinical fact (for "
    "example 'that ADHD-brain thing,' 'it kind of feels like ADHD,' 'my brain "
    "just drops the thread'), and keep it light and relatable. You may use the "
    "topic word so the page is findable, but frame it as a shared experience, "
    "never a claim about the reader's health and never as advice. "
    "Return STRICT JSON only, no prose around it."
)

SCHEMA = """Return JSON with exactly these keys:
{
  "slug": "kebab-case-url-slug, <=70 chars, includes the main keyword",
  "title": "page title under 60 chars, natural, includes the keyword",
  "question": "the exact question this page answers, as a person would ask it",
  "keyword": "the single target keyword phrase",
  "description": "meta description ~150 chars, answers the question directly, includes the keyword",
  "body_markdown": "220-380 words. OPEN with a self-contained 40 to 60 word direct answer to the question, a complete answer a person could quote on its own without the rest of the page. Then continue with short paragraphs and at most one H2 (##). May include one short example text message. No statistics. No em dash. End with at most one honest, non-salesy mention of Clarence if it fits.",
  "faqs": [{"q": "...", "a": "..."}, {"q": "...", "a": "..."}]
}"""


def no_emdash(s: str) -> str:
    # Safety net: the model is told not to, but never ship one. A period reads
    # cleaner than a comma at a clause break, which is where these appear.
    return s.replace(" — ", ". ").replace("—", ", ")


def draft(query: str, ground: str, guidance: str = "") -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY not set (env or the AEO .env)")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    user = (f"{SCHEMA}\n\n{guidance}\n\nQuestion to answer: \"{query}\"\n\n"
            f"Real language to ground it in:\n{ground or '(none available)'}")
    payload = {"model": model, "max_tokens": 2000, "system": SYSTEM,
               "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.load(resp)
    text = "".join(b.get("text", "") for b in data.get("content", []))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise SystemExit("model did not return JSON")
    d = json.loads(m.group(0))
    d["model"] = model
    return d


# --------------------------------------------------------------- stat hard-stop
# The panel's structural safety: a fabricated statistic on a "we don't fake
# anything" brand is the highest-damage failure, and neither the model's own
# prompt nor a human skim reliably catches it. So we detect statistic-shaped
# text deterministically. A hit does not fix the page; it ROUTES it to the
# founder for a number check (over-flagging is fine, a missed fake stat is not).
STAT_RE = [
    re.compile(r"\b\d+(?:\.\d+)?\s*%"),                                  # 73%, 12.5 %
    re.compile(r"\b\d+(?:\.\d+)?\s+percent\b", re.I),
    re.compile(r"\b\d+\s+(?:in|out of)\s+\d+\b", re.I),                  # 3 in 5
    re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten)\s+"
               r"(?:in|out of)\s+(?:two|three|four|five|six|seven|eight|nine|ten|"
               r"hundred|\d+)\b", re.I),                                 # one in five
    re.compile(r"\b(?:studies|research|surveys?|polls?|statistics|data)\s+"
               r"(?:show|shows|found|find|finds|suggest|suggests|say|says|"
               r"indicate|indicates|reveal|reveals|report|reports)\b", re.I),
    re.compile(r"\baccording to (?:a |an |the |one )?"
               r"(?:study|survey|poll|report|research|data)\b", re.I),
    re.compile(r"\b(?:a|an)\s+\d{4}\s+(?:study|survey|report|poll)\b", re.I),
    re.compile(r"\((?:[A-Z][A-Za-z.& ]+,\s*)?(?:19|20)\d{2}\)"),        # (Pew, 2021)
    re.compile(r"\b\d+(?:\.\d+)?\s+(?:million|billion|thousand)\b", re.I),
    re.compile(r"\b\d{1,3}(?:,\d{3})+\b"),                               # 3,000
]


def stat_gate(*texts) -> list[str]:
    hits: list[str] = []
    for t in texts:
        for rx in STAT_RE:
            for m in rx.finditer(t or ""):
                hits.append(m.group(0).strip())
    return list(dict.fromkeys(hits))


# --------------------------------------------------------------- assemble
def yaml_escape(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_markdown(d: dict) -> str:
    fm = ["---",
          f"title: {yaml_escape(d['title'])}",
          f"description: {yaml_escape(d['description'])}",
          f"question: {yaml_escape(d['question'])}",
          f"keyword: {yaml_escape(d['keyword'])}",
          f"updated: {date.today().isoformat()}",
          "aiGenerated: true",
          f"model: {yaml_escape(d.get('model', 'claude'))}",
          "faqs:"]
    for f in d.get("faqs", []):
        fm.append(f"  - q: {yaml_escape(f['q'])}")
        fm.append(f"    a: {yaml_escape(f['a'])}")
    fm.append("---")
    body = no_emdash(d["body_markdown"].strip())
    return "\n".join(fm) + "\n\n" + body + "\n"


# --------------------------------------------------------------- gates + PR
def emdash_gate() -> bool:
    r = subprocess.run(["bash", str(REPO / "scripts" / "no-emdash.sh"), "site"],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        print("EM-DASH GATE FAILED:\n", r.stdout, r.stderr)
    return r.returncode == 0


def open_pr(slug: str, query: str, rel_path: str, hits: list[str]) -> None:
    """Open a one-tap-merge PR from an isolated worktree so we never touch the
    founder's working tree or current branch. Labeled clean or NEEDS-STAT-CHECK."""
    held = bool(hits)
    branch = f"content/answer-{slug}"
    wt = HERE / ".pr-worktree"
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                   cwd=REPO, capture_output=True)
    subprocess.run(["git", "fetch", "origin", "main"], cwd=REPO, check=True)
    subprocess.run(["git", "worktree", "add", "-B", branch, str(wt), "origin/main"],
                   cwd=REPO, check=True)
    try:
        dst = wt / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((REPO / rel_path).read_text())
        subprocess.run(["git", "add", rel_path], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-m",
                        f"content(answers): {query}\n\nAI-generated, labeled on-page. "
                        "One-tap merge to publish."], cwd=wt, check=True)
        subprocess.run(["git", "push", "-u", "origin", branch, "--force"],
                       cwd=wt, check=True)
        if held:
            note = ("\n\n**Needs a number check before merge.** The stat hard-stop "
                    "flagged statistic-shaped text that could be fabricated: "
                    + ", ".join(f"`{h}`" for h in hits)
                    + ". Verify each against a real primary source, or cut it.")
            title = f"answer [VERIFY STAT]: {query}"
        else:
            note = ("\n\n**Stat gate clean:** no statistics detected. "
                    "Review the copy, then merge to publish or close to skip.")
            title = f"answer: {query}"
        body = (f"Auto-drafted answer page for the query: **{query}**\n\n"
                "Written by the content pipeline. Em-dash gate passed." + note)
        subprocess.run(["gh", "pr", "create", "--title", title, "--body", body,
                        "--base", "main", "--head", branch], cwd=wt, check=True)
        print(f"PR opened for branch {branch} ({'NEEDS-STAT-CHECK' if held else 'clean'})")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                       cwd=REPO, capture_output=True)


def record(query: str) -> None:
    data = {"queries": []}
    if STATE.exists():
        try:
            data = json.loads(STATE.read_text())
        except Exception:
            pass
    data.setdefault("queries", [])
    if query not in data["queries"]:
        data["queries"].append(query)
    STATE.write_text(json.dumps(data, indent=2))


# --------------------------------------------------------------- main
def main() -> int:
    load_env()
    args = sys.argv[1:]
    force = None
    if "--query" in args:
        force = args[args.index("--query") + 1]
    voice_id = args[args.index("--voice") + 1] if "--voice" in args else None
    persona_id = args[args.index("--persona") + 1] if "--persona" in args else None
    target = pick_target(force)
    if not target:
        print("no unwritten target in the gap list. nothing to do.")
        return 0
    vid, voice_block = load_voice(voice_id)
    persona = pick_persona(persona_id, target)
    guidance = "\n\n".join(x for x in [voice_block, persona_block(persona),
                                       pinned_language()] if x)
    print(f"target: {target}\nvoice: {vid} | persona: "
          f"{persona['id'] if persona else 'none'}")
    d = draft(target, grounding(), guidance)
    md = to_markdown(d)

    faq_text = " ".join((f.get("q", "") + " " + f.get("a", ""))
                        for f in d.get("faqs", []))
    hits = stat_gate(d.get("body_markdown", ""), faq_text, d.get("description", ""))
    if hits:
        print(f"STAT HARD-STOP: {len(hits)} statistic-shaped item(s) found: {hits}")
        print("  -> routes to you for a number check. A clean page ships without it.")
    else:
        print("stat gate: clean (no statistics detected).")

    if "--dry" in args:
        print("\n----- would write:", ANSWERS_DIR / f"{d['slug']}.md", "-----\n")
        print(md)
        return 0
    ANSWERS_DIR.mkdir(parents=True, exist_ok=True)
    rel_path = f"site/src/content/answers/{d['slug']}.md"
    (REPO / rel_path).write_text(md)
    print(f"wrote {rel_path}")
    if not emdash_gate():
        print("gate failed; leaving the file for inspection, not opening a PR.")
        return 1
    record(target)
    if "--pr" in args:
        open_pr(d["slug"], target, rel_path, hits)
    else:
        print("gates passed. (run with --pr to open the merge PR)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
