#!/usr/bin/env python3
"""Draft one long-form piece from the idea backlog, and open a merge PR.

This is the backlog's drafting arm. Where generate.py makes a short /answers
page, longform.py writes a full piece and opens a one-tap-merge PR:

  - kind "guide": an evergreen, genuinely useful guide (site/src/content/guides).
    Lands ready to publish on merge; the merge is the review.
  - kind "blog": a first-person, build-in-public DRAFT scaffold
    (site/src/content/blog, draft: true). Never a finished founder essay written
    by a bot: it is a starting point for the founder to rewrite in their own
    voice. It stays draft: true even after merge, so it cannot publish until the
    founder flips it by hand. That protects the one thing that matters most here,
    the authenticity of the founder's own voice.

Everything reuses generate.py: the model call shape, the statistics hard-stop,
the em-dash gate, and the worktree-isolated PR so the founder's working tree is
never touched. This never merges.

  python3 longform.py --idea <idea-id>          # draft that backlog card + PR
  python3 longform.py --query "..." --kind guide  # ad hoc, no backlog card
  python3 longform.py --idea <id> --dry         # print, write nothing
  python3 longform.py --idea <id> --no-pr       # write + gate, no PR
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import generate as gen
import ideas as backlog

REPO = gen.REPO
GUIDES_DIR = REPO / "site" / "src" / "content" / "guides"
BLOG_DIR = REPO / "site" / "src" / "content" / "blog"


# --------------------------------------------------------------- prompts
_SHARED_RULES = (
    "HARD RULES: (1) Be genuinely, specifically useful, this is not a sales "
    "page. (2) Never invent statistics, study results, or numbers of any kind, "
    "omit them entirely. (3) Never use an em dash; use a period, comma, colon, "
    "or parentheses. (4) Warm, plain, human, honest, concrete. (5) Mention "
    "Clarence at most once, and only where it genuinely fits, never a hard "
    "pitch. (6) Ground everything in the real language people use, provided "
    "below. (7) For any health or mental-health topic (ADHD, anxiety, burnout, "
    "and the like): never give medical advice, never diagnose, never assume the "
    "reader has the condition. Use the everyday way people talk about it as a "
    "feeling, not a clinical fact, and keep it light and relatable."
)

SYSTEM = {
    "guide": (
        "You write long-form, evergreen guides for Clarence, a private on-device "
        "Mac app that helps people keep up with friends and family by drafting "
        "messages in their own voice from their iMessage history, nothing "
        "uploaded. You are writing ONE guide that fully answers a real question, "
        "the kind someone would bookmark and come back to. " + _SHARED_RULES +
        " Return STRICT JSON only, no prose around it."
    ),
    "blog": (
        "You draft first-person, build-in-public blog posts for Clarence's "
        "founder. Clarence is a private on-device Mac app that helps people keep "
        "up with the people they care about by drafting messages in their own "
        "voice from their iMessage history, nothing uploaded. This is a DRAFT "
        "the founder will rewrite in their own voice, so give them a strong, "
        "honest starting point with a real point of view: a specific opening, a "
        "clear thesis, lived-in detail, and no generic marketing filler. Write "
        "in the first person (I, we). " + _SHARED_RULES +
        " Return STRICT JSON only, no prose around it."
    ),
}

SCHEMA = {
    "guide": """Return JSON with exactly these keys:
{
  "slug": "kebab-case-url-slug, <=70 chars, includes the main keyword",
  "title": "guide title under 65 chars, natural, includes the keyword",
  "question": "the exact question this guide answers, as a person would ask it",
  "keyword": "the single target keyword phrase",
  "description": "meta description ~155 chars, answers the question directly, includes the keyword",
  "body_markdown": "700 to 1100 words. OPEN with a self-contained 40 to 60 word direct answer a person could quote on its own. Then several short sections with ## H2 headings, each practical and specific. Include at least one concrete example text message. No statistics. No em dash. End with at most one honest, non-salesy mention of Clarence if it fits.",
  "faqs": [{"q": "...", "a": "..."}, {"q": "...", "a": "..."}, {"q": "...", "a": "..."}]
}""",
    "blog": """Return JSON with exactly these keys:
{
  "slug": "kebab-case-url-slug, <=70 chars",
  "title": "post title under 70 chars, specific and human, not clickbait",
  "description": "one-sentence dek, ~150 chars, what the post argues or explores",
  "body_markdown": "600 to 900 words, first person. A specific opening (a moment, a decision, a question), a clear thesis, honest detail, and at most one ## H2 if it helps. This is a draft scaffold to be rewritten in the founder's voice. No statistics. No em dash."
}""",
}


def _call_model(system: str, user: str, max_tokens: int) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY not set (env or the AEO .env)")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    payload = {"model": model, "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    text = "".join(b.get("text", "") for b in data.get("content", []))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise SystemExit("model did not return JSON")
    d = json.loads(m.group(0))
    d["model"] = model
    return d


def draft(query: str, kind: str, angle: str, ground: str, guidance: str) -> dict:
    extra = f"\n\nThe angle to take: {angle}" if angle else ""
    user = (f"{SCHEMA[kind]}\n\n{guidance}{extra}\n\n"
            f"Topic to write about: \"{query}\"\n\n"
            f"Real language to ground it in:\n{ground or '(none available)'}")
    return _call_model(SYSTEM[kind], user, max_tokens=4000)


# --------------------------------------------------------------- assemble
def to_markdown(d: dict, kind: str) -> str:
    esc = gen.yaml_escape
    today = date.today().isoformat()
    if kind == "guide":
        fm = ["---",
              f"title: {esc(d['title'])}",
              f"description: {esc(d['description'])}",
              f"question: {esc(d['question'])}",
              f"updated: {today}",
              "faqs:"]
        for f in d.get("faqs", []):
            fm.append(f"  - q: {esc(f['q'])}")
            fm.append(f"    a: {esc(f['a'])}")
        fm.append("---")
    else:  # blog: founder-voiced draft, never auto-published
        now = datetime.now().astimezone().replace(microsecond=0).isoformat()
        fm = ["---",
              f"title: {esc(d['title'])}",
              f"description: {esc(d['description'])}",
              f"date: {now}",
              "draft: true",
              "---"]
    body = gen.no_emdash(d["body_markdown"].strip())
    return "\n".join(fm) + "\n\n" + body + "\n"


# --------------------------------------------------------------- PR
def open_pr(slug: str, title: str, kind: str, rel_path: str,
            hits: list[str]) -> str:
    """Open a one-tap-merge PR from an isolated worktree. Returns the PR URL (or
    '' if gh did not print one). Mirrors generate.open_pr, generalized to the
    guide/blog collections and their different review notes."""
    held = bool(hits)
    branch = f"content/{kind}-{slug}"
    wt = HERE / ".pr-worktree-longform"
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
                        f"content({kind}s): {title}\n\nAI-drafted long-form. "
                        "One-tap merge; the founder's merge is the gate."],
                       cwd=wt, check=True)
        subprocess.run(["git", "push", "-u", "origin", branch, "--force"],
                       cwd=wt, check=True)
        stat_note = ""
        if held:
            stat_note = ("\n\n**Needs a number check before merge.** The stat "
                         "hard-stop flagged statistic-shaped text that could be "
                         "fabricated: " + ", ".join(f"`{h}`" for h in hits)
                         + ". Verify each against a real primary source, or cut it.")
        if kind == "guide":
            title_line = (f"guide{' [VERIFY STAT]' if held else ''}: {title}")
            body = (f"AI-drafted evergreen **guide** for: **{title}**\n\n"
                    "Long-form, em-dash gate passed. Review the substance, then "
                    "merge to publish or close to skip." + stat_note)
        else:
            title_line = (f"blog DRAFT{' [VERIFY STAT]' if held else ''}: {title}")
            body = (f"AI-drafted **blog scaffold** for: **{title}**\n\n"
                    "This is a starting point, not a finished post. It lands as "
                    "`draft: true`, so merging does NOT publish it: rewrite it in "
                    "your own voice, then flip `draft: false` when it sounds like "
                    "you. Em-dash gate passed." + stat_note)
        r = subprocess.run(["gh", "pr", "create", "--title", title_line,
                            "--body", body, "--base", "main", "--head", branch],
                           cwd=wt, capture_output=True, text=True)
        url = ""
        if r.returncode == 0:
            m = re.search(r"https://\S+", r.stdout or "")
            url = m.group(0) if m else ""
            print(f"PR opened: {url or branch} "
                  f"({'NEEDS-STAT-CHECK' if held else 'clean'})")
        else:
            print("gh pr create failed:", (r.stderr or "").strip())
            raise SystemExit(1)
        return url
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                       cwd=REPO, capture_output=True)


# --------------------------------------------------------------- main
def main() -> int:
    gen.load_env()
    args = sys.argv[1:]
    opt = lambda name: (args[args.index(name) + 1] if name in args else None)

    idea = None
    idea_id = opt("--idea")
    if idea_id:
        idea = backlog.get(idea_id)
        if not idea:
            print(f"no backlog idea with id {idea_id!r}")
            return 2
        query = idea["title"]
        kind = idea.get("kind", "guide")
        angle = idea.get("angle", "")
    else:
        query = opt("--query")
        kind = opt("--kind") or "guide"
        angle = opt("--angle") or ""
        if not query:
            print("need --idea <id> or --query \"...\"")
            return 2
    if kind not in ("guide", "blog"):
        print(f"kind must be guide or blog, got {kind!r}")
        return 2

    persona = gen.pick_persona(None, query)
    vid, voice_block = gen.load_voice(None)
    guidance = "\n\n".join(x for x in [voice_block, gen.persona_block(persona),
                                       gen.pinned_language()] if x)
    print(f"drafting {kind}: {query}\nvoice: {vid} | persona: "
          f"{persona['id'] if persona else 'none'}")
    d = draft(query, kind, angle, gen.grounding(), guidance)
    md = to_markdown(d, kind)

    faq_text = " ".join((f.get("q", "") + " " + f.get("a", ""))
                        for f in d.get("faqs", []))
    hits = gen.stat_gate(d.get("body_markdown", ""), faq_text,
                         d.get("description", ""))
    if hits:
        print(f"STAT HARD-STOP: {len(hits)} statistic-shaped item(s): {hits}")
        print("  -> the PR is labeled for a number check.")
    else:
        print("stat gate: clean (no statistics detected).")

    # The guides collection dir is "guides"; the blog collection dir is "blog"
    # (singular). Map explicitly so a blog draft never lands in a stray "blogs/"
    # folder that Astro would ignore.
    coll = "guides" if kind == "guide" else "blog"
    out_dir = GUIDES_DIR if kind == "guide" else BLOG_DIR
    rel_path = f"site/src/content/{coll}/{d['slug']}.md"
    if "--dry" in args:
        print(f"\n----- would write: {rel_path} -----\n")
        print(md)
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    (REPO / rel_path).write_text(md)
    print(f"wrote {rel_path}")
    if not gen.emdash_gate():
        print("em-dash gate failed; leaving the file for inspection, no PR.")
        return 1

    if "--no-pr" in args:
        print("gates passed. (run without --no-pr to open the merge PR)")
        return 0
    url = open_pr(d["slug"], d["title"], kind, rel_path, hits)
    if idea:
        backlog.update(idea["id"], status="in-pr", pr_url=url)
        print(f"backlog: {idea['id']} -> in-pr")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
