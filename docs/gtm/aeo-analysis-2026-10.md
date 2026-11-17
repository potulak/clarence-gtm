# AEO analysis, October 2026: what we did, why it has not worked, what to change

Written 2026-10-17, after a full audit of the measurement ledger plus outside
research into the mechanics of AI answer engines, the agency market, and
consumer (B2C) AI search. Sources are linked inline. Claims are graded where the
grade matters: **[documented]** by an engine operator, **[study]** with a stated
sample, **[practitioner]** opinion, **[vendor]** self-reported marketing.

## The verdict in one paragraph

We automated the two cheapest parts of AEO, writing our own pages and measuring
ourselves, and skipped the only part that causes citations, which is existing off
our own domain. Three months of monthly sweeps have produced a flat 1.7% coverage
number that consists entirely of three queries with our brand name in them. On the
170 unbranded queries we track, we have never been mentioned once. The research
says this is the expected result: unbranded discovery for an unknown product runs
at about 3%, on-page content rewrites have been measured at exactly zero
correlation with fixing that, and the levers that do work are almost all off-site.
Two separate hard blockers also turned up during the audit, both free to fix and
neither previously known.

## Timeline

| Date | What happened |
|---|---|
| 2026-07-03 | `docs/gtm/aeo-geo-playbook.md` written. First 11 guides committed |
| 2026-07-05 | First baseline probe, 525 rows. 3 mentions, all brand queries |
| 2026-07-11 | 22 guides live. All four engines wired. Weekly launchd pulse installed |
| 2026-07-14 | Runtime moved out of `~/Documents` because launchd processes do not inherit Full Disk Access |
| 2026-07-27 | Full sweep. First non-brand citations appear, all on Perplexity |
| 2026-07-28 | Moved to GitHub Actions (`.github/workflows/aeo-weekly.yml`), roughly every 10 days |
| 2026-08-11 | Perplexity key starts returning 401 quota errors. Silent. Still dead |
| 2026-08-18 | Last content published to the live site |
| 2026-08-21 | Anthropic credits run dry mid-sweep. Cadence cut to monthly at about $13 a run |
| 2026-08-21 to 10-16 | Content pipeline generates 15 answer pages. All 15 PRs still open |
| 2026-10-01 | Latest sweep. Coverage 1.7%, unchanged since the first baseline |

Eleven sweeps, 6,149 probe rows, about $36 of API spend, flat the whole way.

## What our own data proves

**Every mention we have ever recorded is a brand query.** All 76 mentions across
all 11 runs come from three queries: `Clarence app review is it legit`, `does
Clarence read my messages`, `Clarence mac app privacy`. On the other 170 queries:
zero, every run. The model is repeating our name back to us.

This is exactly what the literature predicts. The Discovery Gap study (112 Product
Hunt startups, 2,240 queries) found recognition when named at 99.4% on ChatGPT but
discovery on unbranded category queries at **3.32%**, a 30:1 gap, and found that
GEO optimization scores **did not correlate at all** with discovery success
[study, https://arxiv.org/abs/2601.00912].

**The trend report has been reporting its own sampling schedule.** `aeo/trend.txt`
shows mentions going 11, 0, 9, 0, 9 across the last five sweeps. `probe.py`
includes the branded tier only on alternate runs
(`include_branded = (_prior_run_count() % 2 == 0)`), and branded queries are the
only ones that ever hit. The zero runs probed zero branded queries. There is no
signal in that chart at all.

**The probe takes one sample per query per provider.** The best measurement study
in the field (32 prompts, 4 engines, 45 days, up to 10 reruns per prompt in a
24-hour window) found a single run has a standard error of about **0.37** for
per-brand detection, needs about **7 runs per prompt per day** to get below 0.10,
and that reruns within the same 24 hours are nearly as unstable as reruns across
days, which means most of the noise is model randomness rather than real change
[study, https://arxiv.org/html/2604.07585v1]. Our n=1 design cannot detect
anything smaller than the gap between zero and obvious.

**Two hard blockers, both found during this audit.**

1. **Bing does not have the site.** Three verbatim phrases from our own pages each
   return "There are no results" on Bing, while Google has 45 URLs indexed. Bing
   is OpenAI's documented primary web source for ChatGPT search and is the whole
   index behind Copilot, so two engines cannot retrieve us regardless of what we
   publish.
2. **Perplexity has been blind since 2026-08-11**, 859 failed calls. It was the
   only engine that ever produced a non-brand citation. The failure is silent:
   `probe.py` writes `ok=0` and the digest still prints a clean report.

Both are in `docs/URGENT.md`.

**The substrate we never touched, from our own `sources.json`.** Across 4,093
citation events: reddit.com 9.9%, apps.apple.com 5.0%, youtube.com 3.3%,
alternativeto.net 0.8%, and a long tail of rival-owned listicles (`crm.org`,
`wavecnct.com`, `yourpond.io`, `storyflow.so`, `bigcontacts.com`,
`getdex.com/blog/best-free-personal-crm`). Clarence has no App Store listing, no
AlternativeTo entry, no Product Hunt, no YouTube, no Reddit presence, no press, no
reviews, and appears in nobody's listicle. A web search for the brand returns
nothing. Our own playbook has an "Off-site" section naming most of this. It was
never executed.

**Competitor share of voice, computed for the first time.** Percentage of answers
naming each brand, from the raw transcripts:

| Brand | 08-01 | 08-11 | 08-21 | 09-01 | 10-01 |
|---|---|---|---|---|---|
| Dex | 4.8% | 3.9% | 5.2% | 3.7% | 4.6% |
| Monica | 4.2% | 4.1% | 4.1% | 3.5% | 4.4% |
| Clay | 2.9% | 3.5% | 4.1% | 2.1% | 3.0% |
| Catchup | 3.0% | 2.0% | 1.6% | 0.5% | 2.2% |
| Covve | 1.6% | 1.2% | 1.9% | 1.6% | 2.2% |
| Garden | 2.5% | 2.5% | 2.1% | 1.2% | 1.8% |
| UpHabit (shut down Apr 2026) | 0.4% | 1.4% | 1.4% | 0.5% | 1.2% |
| Kinu | **0.0%** | 0.4% | 0.6% | 0.7% | **1.2%** |
| Clarence, unbranded | 0% | 0% | 0% | 0% | 0% |

Two things in that table matter more than everything else in this document.
**Kinu went from absent to 1.2% in two months**, from the same cold start we are
in, in the same category, measured by our own harness. And **UpHabit shut down in
April 2026 and is still named in 1.2% of answers**, which is live demand with a
dead incumbent that nobody is capturing.

Kinu is worth studying directly (https://kinu.care/blog, 55 posts). Its structure
is comparison pages (`/dex-vs-kinu`, `/clay-vs-kinu`, `/notion-personal-crm`),
alternative and shutdown capture (`/uphabit-alternatives`,
`/personal-crm-apps-shut-down`, `/monica-crm-alternative`), problem-first pages,
and citable reference pages (`/friendship-recession`, `/dunbars-number-relationships`).
It also already holds `/private-personal-crm`, "Most Private Personal CRMs,
Local-First, No Cloud Required", which is our positioning. That slot is contested,
not empty.

## Why it is not working: five root causes

**1. We aimed at the hardest queries and skipped the easiest.** Our 173 queries
are 56 "how" queries and only 3 "alternative" plus 1 "vs" query. Broad
informational queries are owned by Reddit, Quora, YouTube and established media.
The one controlled experiment on self-authored content found a brand-new entity
captured **72 previously empty query slots with 82% of the gain coming from its own
pages**, but with presence of 66.4% on a narrow exact-match query versus **15.8%**
on the broad version of the same topic [study, Ahrefs, 34 pages, 9,886 answers,
Feb to May 2026, https://ahrefs.com/blog/self-promotional-content-ai-seo-experiment/].
We spent our content budget on the 15.8% end.

**2. Retrieval, not writing, is the gate, and retrieval is off-site.** The best
mechanistic account (34,960 unbranded prompt-engine observations, 2,854 prompts)
models mention as a staged process and measures the stages: with no brand or
own-domain exposure in the retrieval path, mention rate is **2.8%**; with
own-domain citation it is **49.0%**; with a branded fan-out as well, **91.4%**
[study, https://arxiv.org/abs/2609.23162]. Separately, content match predicts
exposure only weakly (AUC 0.545 on GPT). Writing better pages improves how a
document is used once retrieved. It does very little to get retrieved.

**3. Visibility is self-reinforcing, so the cold start is a barrier and not a
phase.** In the same study, **prior-run history alone predicts next-run mention
nearly as well as the full model** (AUC 0.937 versus 0.963). Prior mention plus
exposure gives an 80.5% next-run mention rate; prior non-mention with no exposure
gives **1.6%**. We have been at 1.6% for three months and the system is designed to
keep us there until something external breaks the loop.

**4. Two of the four engines could not see us, and one of our probes could not
see them.** See the blockers above.

**5. Part of the work we did do is documented theater.** Schema markup: the only
matched difference-in-differences test (1,885 pages adding JSON-LD against about
4,000 matched controls) found AI Overviews **-4.6%**, AI Mode +2.4% and ChatGPT
+2.2%, the last two indistinguishable from noise
[study, https://www.searchenginejournal.com/schema-markup-didnt-move-ai-citations-in-ahrefs-test/574568/].
llms.txt: **97% of llms.txt files received zero requests** in May 2026, and SE
Ranking across roughly 300,000 domains found no citation effect
[study, https://originality.ai/blog/llms-txt-tracking-study]. John Mueller: "no AI
system currently uses llms.txt", and Google's own AI-features docs say you do not
need any AI text file [documented,
https://developers.google.com/search/docs/appearance/ai-features]. We built both.

The one technical item with real evidence is server-side rendering, because
GPTBot, OAI-SearchBot, ClaudeBot and PerplexityBot do not execute JavaScript. We
already pass that, verified during this audit.

## What the evidence says actually works

Ranked by measured effect size, strongest first.

**Review-profile existence is the largest measured threshold effect in the
literature.** Seer, 804,491 AI responses, 1,926 brands, 4 platforms, 8 verticals,
15,783 prompts: median citation rate **1% with no verified Trustpilot profile,
53.5% with a minimal profile of as few as 1 to 13 reviews**. The step appeared in
every vertical tested. Most importantly for us: **brands at DR 0 to 15 with a
minimal profile reached 54.5%, essentially matching DR 16 to 35 brands at 52.6%**,
so the threshold substitutes for domain authority at the bottom end. Review and
trust sites are the second-largest citation source overall at 14.01%, rising from
1.51% at awareness to 24.27% at purchase intent
[study, https://www.seerinteractive.com/insights/study-of-800k-ai-responses-how-reviews-shape-brand-presence-in-ai-search].
Correlational, single vendor, and Seer names marketing spend as a possible
confound. Even heavily discounted this is the best return per hour available.

**Branded web mentions anywhere, not backlinks.** Ahrefs, 75,000 brands, Spearman
correlations with AI Overview visibility: branded web mentions **0.664**, branded
anchors 0.527, branded search volume 0.392, domain rating 0.326, referring domains
0.295, **backlinks 0.218**, site pages 0.170. Top-quartile brands by web mentions
averaged 169 AIO mentions against 14 for the next quartile
[study, https://ahrefs.com/blog/ai-overview-brand-correlation/]. A separate
consumer-focused Ahrefs study of 75,000 brands put **YouTube mentions highest of
any factor at about 0.737**, with 94% of YouTube citations coming from long-form
video rather than Shorts [study, https://ahrefs.com/blog/how-to-rank-on-chatgpt/].

**Ranked "best of" listicles carry a fifth of all citations.** Ahrefs, 26,283
source URLs across 750 top-of-funnel prompts: **blog lists are the most-cited page
type at 43.83%** against 14.34% for ordinary blog posts, and across 250 "best X
software" SERPs, **169 (67.6%) contained a list where the publisher ranked itself
first**. Self-ranking is the category norm. A separate study across 100,000-plus
responses put ranked listicles at about 21% of all citations
[study, https://arxiv.org/abs/2606.20065].

**Answer-first structure is the one content lever with coherent mechanism
evidence.** The citation-failure taxonomy (949 contrastive pairs) attributes
**62.2% of failures to semantic alignment** (intent divergence, outdated
information), 27.1% to content quality (scarcity, fragmentation, verbosity,
unstructured layout) and only **10.1% to technical integrity**
[study, https://arxiv.org/html/2603.09296v1]. Our pages already do this reasonably
well, which is why it is not where the remaining upside is.

**Publishing pricing, and answer-first restructuring, are the cheapest reported
wins.** From a practitioner survey (n=169): structure changes were the single
most-cited win; review listings produced AI mentions within a week; one respondent
saw more AI mentions within two days of publishing pricing
[study, https://www.commonmind.com/blog/state-of-ai-visibility-in-b2b-saas]. We
already publish pricing.

**What does not work, with numbers.** Press: Seer measured the correlation between
frequent major-news mentions and ChatGPT brand mentions at about **0.07**. Social
media: 70% of practitioners do it, 22% believe it works. Podcasts: 36% do, 7%
believe. Quora: 0.0% cross-engine portability. PR and earned media is the only
tactic where belief equals effort, at 46/46.

**A constraint specific to us.** The single largest citation source for app
recommendation queries is the App Store. AppTweak, 125,000-plus ChatGPT
recommendation responses across 9,489 app prompts: **Apple App Store 37.6%**,
Google Play 9.5%, app stores combined **47.5%**
[vendor with stated method, https://www.apptweak.com/en/aso-blog/ai-app-discovery-llm-search].
**That lever is structurally closed to Clarence.** The Mac App Store sandbox
forbids reading `chat.db`, decided in
`docs/decisions/0023-scalable-product-is-a-mac-app.md`. We cannot have the thing
that supplies nearly half of app-recommendation citations. This is the most
important strategic fact in this document, and it means our off-site plan has to
over-index on the surfaces we can reach: AlternativeTo, review platforms, YouTube,
listicles and genuine community presence.

## What to change

Ordered by expected return per hour, with the cheap and certain first.

**Now, this week, all free.**

1. **Merge the 15 open answer PRs** (#28, #31 through #44). Each is a single
   content file, about 30 lines, no deletions, no CI, no conflicts. The site serves
   10 answer pages when 25 exist. This is the lowest-effort action in the document
   and has been waiting since 2026-08-21.
2. **Register Bing Webmaster Tools**, submit the sitemap, request indexing on the
   top pages, enable IndexNow. This unblocks ChatGPT and Copilot at the retrieval
   step and is also the only first-party AI citation reporting that exists anywhere:
   its AI Performance report exposes per-page citations and the "grounding queries"
   the assistant actually wrote, which no other engine gives at any price.
3. **Add credits to the Perplexity key** and make the sweep fail loudly when any
   provider's ok-rate drops below about 90%, so a dead key cannot hide for two
   months again.
4. **Create the review profiles.** Trustpilot first, given it is the measured one,
   then AlternativeTo (add Clarence, then list it as an alternative on the dead-app
   pages: `garden-stay-in-touch`, `keep-my-friends`, `friendly-reminder`,
   `intact--personal-crm`, `queue-personnal-crm`, `meerkat-crm`). Capterra and
   GetApp are free but check category fit first, since personal apps are sometimes
   excluded. This is the 1% to 53.5% threshold and it costs an afternoon.

**Next, the content pivot.**

5. **Reaim the content at narrow comparison and alternative queries.** We have 3
   "alternative" and 1 "vs" page against Kinu's dozens. Write, honestly and naming
   competitors: `clarence-vs-monica`, `clarence-vs-dex`, `uphabit-alternatives`
   (dead incumbent, 1.2% of answers, nobody capturing it), `monica-crm-alternative`
   (we have this one), `garden-stay-in-touch-alternative`, `personal-crm-for-mac`,
   `private-personal-crm-no-cloud`, `catchup-app-alternative`. Expect the backfire
   the experiment measured: the brand was **skipped in 43% of answers that cited its
   own page**, because the page earns the citation and a competitor listed on it
   earns the recommendation. That is still a far better position than invisible.
6. **Budget for refresh, not publication.** Median citation half-life is **4.5
   weeks** (3.5M citation events; ChatGPT fastest at 3.4 weeks, Perplexity longest
   at 5.8). About a quarter of self-promotional pages in the Ahrefs experiment were
   cited once and never again. A page published and forgotten is cited roughly one
   day in three. This is maintenance work, not a project.
7. **Buy long-form YouTube reviews.** Highest-correlated single factor at about
   0.737, and 94% of YouTube AI citations are long-form rather than Shorts. Market
   rate is roughly $100 to $300 for an honest flat-fee review (Collabstr averaged
   about $255 per YouTube collaboration in 2025; Teleprompter pays a flat $100
   regardless of subscriber count). Three or four of these is the cheapest credible
   off-site move available to us.
8. **Pitch TechRepublic.** Across every roundup examined,
   https://www.techrepublic.com/article/best-personal-crm/ is the only genuinely
   independent "best personal CRM" list. Everything else in the category
   (Dex, Kinu, amiqo, folk, YourPond, OnePageCRM, Wave Connect, Inner Circle,
   BigContacts) is published by a competitor that ranks itself first. There are
   almost no editorial slots to pitch here, which cuts both ways: outreach barely
   works, and self-authored lists still land.

**Rebuild the measurement, because the current numbers cannot support a decision.**

9. **Separate branded from unbranded coverage and never sum them.** Report them as
   two numbers. Label any run where a tier was skipped.
10. **Raise sampling to 7 or 8 runs per query per provider** on a small unbranded
    core set, and report rolling 2 to 4 week windows. Keep the set small to afford
    it: prompt count is the wrong dial, since paraphrasing the same intent changes
    the returned brand set *more* than rerunning the identical prompt does (Jaccard
    0.135 to 0.288 for rewordings against 0.50 to 0.61 for identical reruns)
    [study, https://arxiv.org/abs/2605.27440].
11. **Do not force web search.** Let the model decide and record each prompt's
    grounding rate as a first-class metric. Forcing shifts visibility by about 20
    points on prompts that rarely search naturally, where sampling noise accounts
    for only 3.4 to 4.1 points
    [study, https://graphite.io/five-percent/research/do-not-force-search-prompt-tracking].
    Our `probe.py` passes web-search tools unconditionally on all four providers,
    so this needs changing.
12. **Track competitor share of voice as the primary metric**, which the harness
    can already compute from the raw transcripts and never did. Our own mention
    count has been zero for three months and carries no information. Dex at 4.6%
    and Kinu's climb from 0.0% to 1.2% carry a great deal.
13. **Report per engine, never blended.** Cross-engine URL Jaccard is 0.11 to 0.18
    across 11,500 queries, and only 2.37% of cited URLs appear in all three engines
    for the same prompt, with 91.07% appearing in exactly one
    [study, https://www.growth-memo.com/p/the-consensus-gap]. A single AI-visibility
    score is not a measurement. Kevin Indig's three replacement metrics are worth
    adopting verbatim: Presence, Portability, Concentration.
14. **Add a "how did you hear about us" field to the waitlist form.** Crude, and
    the most honest attribution instrument that exists. ChatGPT mentions brands
    about 3.2x more often than it links to them, so most of the value is structurally
    invisible to referrer-based analytics.

**What to stop.** Stop expecting anything from llms.txt or schema (keep both, they
cost nothing, claim nothing for them). Stop the monthly four-engine full sweep at
$13 while a quarter of it errors; spend that budget on deeper sampling of a
smaller unbranded set. Stop treating the trend chart as a signal.

## Honest expectations

Set these before doing any of the above, because the research is consistent and
unflattering.

- **Six months, not 90 days**, for first measurable movement off zero from a cold
  start. The one vendor case study of a new domain reached 61 AI-cited pages in six
  months, and it was self-published by the firm selling the service.
- **Unbranded category discovery may stay close to unwinnable** for a while. The
  numbers are 3.32% for an unknown product, and the self-reinforcement finding says
  that does not drift upward on its own.
- **Branded queries are winnable almost immediately**, and we already win them. A
  fake-brand experiment reached near-exclusive visibility in under 30 days, but
  **96% of that visibility came from branded searches**.
- **Do not build a business case on AI referral traffic.** It is 0.2% to 1% of
  sessions across every study measured, only about 7% of US ChatGPT responses carry
  citations at all, and the July 2026 critical survey of 45 studies rates "citation
  scores predict clicks, conversions, or revenue" at **very low** evidence while
  explicitly rejecting the widely-quoted "GEO increases visibility by 40%" claim as
  a general statement [study, https://arxiv.org/abs/2607.14035]. The value of this
  work is being named in a zero-click answer, which is real and nearly
  unattributable.

The strategic question this raises, and it is worth asking plainly: given the App
Store lever is closed, the category's citation substrate is written almost entirely
by competitors, and honest expectations are six months to first movement on a
metric that does not predict revenue, AEO probably should not be the primary
acquisition channel for Clarence. It is worth the cheap items in the "Now" list,
which are hours of work with measured effects. It is not worth another three months
of content and measurement at the current allocation.

## Sources

Linked inline above. The three heaviest: the July 2026 critical survey of 45
studies (https://arxiv.org/abs/2607.14035), the staged mention model
(https://arxiv.org/abs/2609.23162), and the measurement-variance study
(https://arxiv.org/html/2604.07585v1). Our own ledger is `aeo/results.csv`,
`aeo/sources.json` and the raw transcripts under `aeo/raw/`.
