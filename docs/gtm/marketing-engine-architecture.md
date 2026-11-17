# The marketing engine: cloud, data, and agent architecture

**Status:** system design (2026-07-29). Companion to [autonomous-marketing-engine.md](autonomous-marketing-engine.md), which covers strategy, governance tiers, and lanes. This doc covers the three things the founder pressed on next: run it in the cloud so his Mac is never the source, make every decision data-backed, and lay out the agent topology in a way that is protected.

## 1. Where it runs (your Mac is out of the loop)

The rule is simple: **the founder's Mac is a viewing device, never a dependency.** Nothing the engine needs to run may require his laptop to be awake, logged in, or holding Full Disk Access. The content pipeline already proved why: a launchd job under `~/Documents` is silently blocked by the OS and has shipped one page in weeks.

Your own decision records draw the split for us. ADR-0026 makes Cloudflare the one cloud for data and notes that Workers' short CPU budgets "rule out heavyweight" compute. So:

- **Data and always-on edge: Cloudflare.** D1 is the marketing data warehouse. R2 holds raw blobs (answer transcripts, ad creative, screenshots). KV holds fast state and the approval queue. Workers host the always-on endpoints (approval inbox, incoming webhooks, the digest sender) and Cron Triggers fire the light, fast collectors. This is the sanctioned one cloud, and it is where data lives so it is never on your Mac.
- **Heavy scheduled agent compute: GitHub Actions cron.** The LLM-heavy jobs (drafting, review, the weekly brain) run as scheduled Actions, exactly like the AEO sweep already does. They read and write the Cloudflare warehouse over HTTPS. Actions is the right home because it is proven here, essentially free at this scale, and comfortable with multi-minute LLM orchestration that Workers cannot do.

**Is GitHub a second cloud that breaks ADR-0026?** No. ADR-0026 governs where *product and user data* live, and that stays entirely in Cloudflare. GitHub already hosts the code. Using Actions as an ephemeral compute runner that holds no durable data (it reads and writes D1, keeps nothing) is CI, not a data platform. The marketing warehouse is also strictly separate from anything relationship-related: it holds only public data, the waitlist (email plus source plus preferences, already no IP or user agent), and marketing artifacts and metrics. It has zero access to `chat.db` or the on-device dossier. ADR-0028 (relationship data never egresses) is untouched.

**Cost.** Compute is effectively free: GitHub Actions has a generous free tier for short jobs, and Cloudflare's D1, Workers, KV, and R2 free tiers are far above what this needs. The only real costs are Anthropic tokens (a few dollars a day if we use cheap models for collectors and drafts and reserve the expensive model for the weekly brain) and Meta ad spend (capped by you). That is the cheapest viable shape.

## 2. Everything is data (the spine)

This is the core of the request: not dashboards bolted on at the end, but a data spine that every lane writes to and every decision reads from, so nothing is decided on a hunch. The design principle is a **closed, self-grading loop**: the engine records what it did, what it expected to happen, and then grades itself against what actually happened.

### The event model (Cloudflare D1)

Four core tables, append-only where it matters, one source of truth:

- **`signals`** every observation, normalized to one shape (`ts`, `source`, `kind`, `entity`, `value`, `meta`). It absorbs: AEO mention / position / citation per query per engine, Cloudflare traffic and geography, Reddit thread opportunities and mined ICP pains and wants, ad impressions / clicks / CTR / cost-per-lead from Meta insights, waitlist signups with source and the preference answers, and email opens if we ever send. One table, one query surface.
- **`actions`** everything the engine did (`ts`, `lane`, `type`, `tier`, `ref`, `tokens`, `status`): content page published, Reddit draft queued, Reddit post approved and posted, ad launched, budget changed, email sent. Every action carries a unique **tracking tag** (a UTM or campaign id) so downstream signups attribute back to the exact action that caused them.
- **`outcomes`** the join that makes attribution real: this content page produced these AEO position gains and these signups; this ad produced these clicks and these signups at this cost each. This is where cost-per-signup per lane per campaign becomes a number, not a guess. `classifySource` already buckets signups by origin; the tracking tags close the last mile.
- **`decisions`** the weekly brain's calls, each stamped with the data it read, the action it chose, the effect it predicted, and a review date. Next cycle the brain grades the prediction against the actual outcome and adjusts. Over time the engine learns which communities, messages, and lanes produce signups per dollar, and the founder's digest shows the data behind every recommendation.

### Why this shape

- **One warehouse, not three ledgers.** Today AEO data is split across a committed `aeo/` folder and a local runtime that drift. Moving the spine into D1 kills the drift and takes the data off your Mac in one move.
- **Structured output only.** Every agent writes rows, not prose. "Everything is data" is enforced at the seam: an agent's job is not done until it has emitted valid structured output to the warehouse.
- **Self-grading is the differentiator.** Most "data-driven" marketing stops at reporting. Recording predictions and grading them is what lets the engine actually get better each week instead of just busier.

## 3. Agent topology (forms and sub-agents)

Five agent families, each a charter in the shape your panel already uses (a grounding block, an ordered objective, its warehouse inputs, hard constraints, and a required output schema). Diversity comes from the charter, not the model, so we run cheap models everywhere except the brain.

- **Collectors (Tier 0, read-only).** One per source: AEO, traffic, Reddit listener, ad-metrics, funnel. They pull, normalize, and write `signals`. Mostly non-LLM or light LLM (only ICP mining needs a model). They run as Cloudflare Cron Triggers (fast) or Actions (heavier), and they hold **read-only** credentials.
- **Makers.** Content writer, Reddit reply drafter, ad-creative generator, site-copy proposer. LLM agents grounded by RAG over the warehouse (the ICP corpus plus what is currently winning). Each tags its output with a hypothesis and a target metric. Critically, Makers hold **no credentials that can touch the outside world.** They only write drafts into the warehouse.
- **Reviewers (adversarial, one per Maker).** Skeptic-form, default reject. Content reviewer (voice, no invented stats, useful, em-dash gate), Engage reviewer (genuine presence, no fake questions, disclose when naming Clarence), ads reviewer (claims discipline, sane targeting and budget). A Maker's output advances only if its Reviewer passes it.
- **Orchestrator / Brain (weekly).** Reads the warehouse, grades last week's `decisions`, may convene the product-debate panel for strategy calls, then writes next week's plan and the founder's digest. This is the only place we spend the expensive model.
- **Dispatcher (executes approved Tier 2 actions).** After the founder taps approve, this is the one agent that posts the Reddit comment, launches the ad, or sends the email. It holds the only world-write credentials, runs only on already-approved items, and enforces rate limits and the spend cap in code.

The important structural choice: **only the Dispatcher can act on the world, and it only acts on what a human approved.** Everything upstream reads untrusted public data and produces drafts. That containment is what makes the security model hold.

## 4. Protected (the security model)

The engine ingests untrusted public content (Reddit threads, web pages, ad comments) and holds credentials that can spend money and post publicly. Both are real threats. The model:

- **Credential isolation by tier.** Collectors get read-only tokens. Makers get no world-write tokens at all. Only the Dispatcher holds tokens that can post or spend, and they are least-privileged: the Meta token scoped to one ad account under a hard cap, a narrowly scoped Reddit token separate from the listener's read-only one. A compromise of a Maker cannot post or spend, by construction.
- **Prompt-injection containment.** A malicious Reddit post may try to instruct the drafter. Three defenses stack: ingested content is treated as data and never as instructions (the same boundary the assistant itself runs under), the adversarial Reviewer is a second independent check, and the human approval tier gates anything public. Makers have no tools that can act even if injected.
- **Caps and kill-switch as code.** The ad budget and posting rate limits live in the Dispatcher, not in a policy doc. A single KV flag can halt all world-write actions instantly. ADR-0026's proxy already models this pattern (a KV kill switch, opaque revocable tokens).
- **Secrets never in the repo or the warehouse.** Keys live in GitHub Secrets and `wrangler secret`, injected at runtime into ephemeral jobs, never committed, never written to D1.
- **Data separation is physical.** The marketing warehouse is a different D1 database from anything relationship-related, and the engine has no path to `chat.db` or the dossier. Relationship data never egresses; this engine never even sees it.
- **PII minimization.** The waitlist keeps its existing discipline: email and source, no IP, no user agent. Agents may never export the email list anywhere except the send-only email vendor. Collectors can aggregate funnel metrics inside a Worker so Actions only ever reads aggregates, not raw emails.
- **Audit log.** Every action and its approval is an immutable row in `actions`, so there is always a full trace of what the engine did and who let it.
- **Supply chain.** Pinned dependencies, and the standing repo rule that any new download needs explicit per-item confirmation.

## 5. Cost control (never out of control)

The founder's hardest constraint: he wants to see cost at all times, know when a limit is close, and never be surprised by a bill. So cost is a first-class subsystem, not an afterthought.

- **Meter everything, into the spine.** Every agent run records its token spend (per lane, per model, per day) to the `actions` table, building on the AEO engine's existing `cost.py`. The Meta collector records ad spend. A light job records infrastructure usage against each free-tier limit (D1 rows and reads, Workers requests, GitHub Actions minutes, R2 storage). Nothing spends silently.
- **Caps you set once.** A monthly token budget and a daily soft cap for Anthropic, the ad cap for Meta (about one dollar a day, Meta's practical floor, hard-ceilinged at twenty dollars for month one, your authorization), and the free-tier thresholds for infrastructure. All in config, all visible.
- **Tiered alerts and projection.** Green under a threshold, amber approaching it, red at or over. Crucially, the digest shows a run-rate and a projected month-end next to the cap, so trouble is visible days before it lands, not after.
- **Guardrails as code, not as hope.** Non-essential lanes auto-pause at ninety percent of any cap, and hard-halt with a red alert at one hundred percent. Every agent run carries a token ceiling (the workflow runtime already supports a per-run budget). A single KV flag halts all spend instantly, the same kill-switch pattern ADR-0026 uses for the proxy.
- **Cheap by default keeps the baseline low.** Cheap models for collectors and drafts, cached grounding context, batched collectors, the expensive model only for the weekly brain. At this scale the infrastructure is effectively free and the token spend is a few dollars a day, but the meters and caps are what keep it that way as volume grows.

The cockpit surfaces this as its own panel (spend against cap, run-rate, projection, free-tier headroom), so a glance answers "are we close to a limit, and how much more."

## 6. What this means to build (delta on Phase 0)

The strategy doc's Phase 0 grows one item: **stand up the Cloudflare data spine and move the source of truth off your Mac.** Concretely:

1. Create the marketing D1 database and the four tables. Point the AEO sweep and the collectors at it (retire the split ledger).
2. Move the content pipeline's schedule from FDA-blocked launchd to GitHub Actions cron, reading gap targets from D1.
3. Stand up the Worker approval inbox and the digest sender, backed by KV.
4. Write the agent charters (Collectors, Makers, Reviewers, Dispatcher) and the shared structured-output contract.

After that, every later phase is just turning on lanes that already read and write a warehouse that runs without you.
