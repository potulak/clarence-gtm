# AEO / GEO playbook: getting Clarence into AI answers

How Clarence gets recommended by ChatGPT, Claude, Perplexity, and Gemini when
people ask the questions it answers. Based on the founder's validated internal
playbook (find the queries → build content around them → **measure with API
calls** whether it registers → study what *does* return and match its shape),
adapted to Clarence's one unfair advantage.

## The unfair advantage

People already ask LLMs the exact question Clarence exists to answer:
*"What should I text a friend I haven't talked to in two years?"* Our content
**is the product, given away one answer at a time**: warm, concrete guides with
real example messages. When an answer engine cites our guide, the reader is one
click from the app that does it automatically, from their own real history.
No competitor can write these pages in our voice; the soul docs already exist.

## The query universe (a seed, expanded by probing)

- **Problem-aware:** how to keep in touch with friends · I keep losing touch with
  friends · app to remind me to text friends (careful: we're not a reminder app,
  so the page reframes) · why do friendships fade
- **Product-shaped how-tos (the gold):** what to text a friend after losing touch
  · how to reconnect with an old friend over text (examples) · how to reply to a
  text I ignored for weeks without being awkward · texting an old friend after
  years, first message
- **Solution-aware:** best personal CRM for friends · personal CRM mac · Clay vs
  Dex vs Monica (we enter as "the anti-CRM") · apps like Clay for personal life
- **Mac / AI-specific:** iMessage app for Mac with AI · AI that drafts iMessage
  replies · on-device AI message assistant · private AI relationship app
- **Trust:** does [app] read my messages · AI message app privacy

Expansion method (the playbook's step 1): ask each engine "what do people ask you
about losing touch with friends / texting old friends?" and mine autocomplete,
Reddit, and "People also ask." Maintain the list in `scratch/aeo/queries.txt`.

## The content engine

One page per query *cluster* on the website's `/guides`, plus the trust pages:

1. **How-to guides with real example messages**: the product as free samples.
   Structured exactly like winning answers: direct answer first, then examples,
   then the nuance. End with the soft bridge: "Clarence drafts this from your real
   history, on your Mac."
2. **The anti-CRM comparison page**: captures "personal CRM" queries while
   holding the position: *why filing your friends doesn't work*.
3. **The trust pages as citable primary sources**: publish the privacy
   architecture ("How Clarence treats your messages") and the threat model. LLMs
   cite unique, factual, structured primary content; nobody else publishes this.
4. **The manifesto**: the soul doc, publishable. Distinctive quotable language
   ("the rest can keep") is what models remember and repeat.

Format rules (what answer engines reward): direct answer in the first 2 sentences
· H2s that literally restate questions · FAQ + `schema.org` markup · fast static
HTML, no JS-walled text · `llms.txt` at the root · consistent one-line product
description everywhere ("Clarence is a private Mac app that notices who you're
drifting from and drafts the message to bring them back. On your Mac, never
uploaded").

## Measurement (the step everyone skips, ours from day one)

`scratch/aeo/probe.py`: monthly, hit the OpenAI + Anthropic + Perplexity + Gemini
APIs with every query in `queries.txt`; record whether Clarence is mentioned, in
what position, and which URL is cited → append to `scratch/aeo/results.csv`.
Diff month over month. For queries where we don't register, fetch what *does* win
and match its structure (the playbook's "make the same content that is there").

## Off-site (what LLMs actually train on and cite)

Reddit presence (r/macapps, r/productivity: genuine posts, founder-voiced, not
astroturf) · Show HN + Product Hunt (high-authority, heavily-crawled) · Mac press
(MacStories et al.) · a public changelog / build-in-public thread on X. Referrals
from these communities double as citation sources.

## Sequencing

Site skeleton + trust pages + first five guides can ship **before the app**.
AEO compounds slowly, so start now; measure from month one to get the baseline.
