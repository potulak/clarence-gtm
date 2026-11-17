# The content pipeline (AEO amplification engine)

The flywheel that turns "a question people ask an answer engine" into "a
published, verified guide that gets cited." Measurement tells us what to write;
this pipeline writes it, checks it, and puts it one tap from live.

**The gate is a PR.** Nothing publishes itself. A cycle drafts, self-checks, and
opens a pull request; the founder skims and merges from his phone; merge
deploys. That merge is the 1%. Everything before it is the 99%.

## The loop

1. **Measure.** The AEO probe runs on its cadence (weekly watchlist pulse +
   monthly full sweep; see the runtime under `~/Library/Application
   Support/clarence-aeo/`). `digest.py` produces the gap signal: questions we're
   absent from everywhere, and the domains winning them.
2. **Pick.** Take the highest-value uncovered query (high purchase-intent
   first, then high-volume informational). One page per cycle beats five thin
   ones.
3. **Research + verify.** Gather the facts, then verify EVERY number against
   its original primary source. This is the non-negotiable rule (see below).
4. **Draft.** Write the guide in the founder's voice, arm it with a verified
   `StatChart` where data helps, wire internal links.
5. **Self-check.** Run the gates (below). Anything that fails never reaches
   the PR.
6. **PR.** Open a pull request with the guide, its sources, and a checklist.
7. **Merge, then deploy.** Founder merges; deploy ships it. Next cycle the probe
   measures whether it moved.

## The hard rules (these are what make automation safe here)

Trust is the product. On a site asking to read people's messages, one made-up
stat or one paragraph that reads as AI does outsized damage. So:

- **Verify every number against its ORIGINAL source before it ships.** Never
  quote a statistic from memory or from secondary reporting. Read the primary
  document (the report PDF, the journal paper, the government advisory), quote
  it, link it. If a figure can't be verified at its primary source, it does not
  go in. See `[[site-data-graphs-engine]]` for the already-verified figures and
  their sources; reuse those freely, add new ones the same way.
- **No em dashes, anywhere.** Enforced by `scripts/no-emdash.sh` (`npm run
  check:copy`). See CLAUDE.md.
- **Human voice, not AI voice.** Simpler is always better. Short sentences. Cut
  words. Let a thought land. Break the frictionless symmetry that reads as
  machine-written: no over-balanced tricolons, no "it's not X, it's Y"
  scaffolding, no tidy summarizing kicker, no uniform sentence rhythm, no
  colon-chaining. A touch of first-person honesty where it earns trust. See the
  de-AI direction memory.
- **Useful whether or not they install Clarence.** Every guide answers the
  question fully and honestly first. The product is a quiet footer, never the
  point of the page.
- **Clarence, not the category.** Don't claim a crowded market. The real
  contrast is personal CRMs ("a spreadsheet you fill in yourself"): manual and
  tedious, where Clarence notices drift for you and is about friendships, not a
  contact database.

## Guide shape (the content collection contract)

One file per query cluster in `site/src/content/guides/`, `.md` or `.mdx`
(use `.mdx` when embedding a `StatChart`). Frontmatter: `title`, `description`,
`question` (the query verbatim, becomes the H1), `updated`, `faqs` (rendered
on-page AND emitted as FAQPage schema). Body: direct answer in the first two
sentences, then the depth. Quote scalars with `: ` in them (breaks Astro's
content sync otherwise). Charts: `import StatChart` (and `DunbarLayers` for the
rings); see any of the five data guides for the pattern.

## The gates (run every one before opening the PR)

From `site/`:

- `npm run check:copy`: the em-dash gate.
- `npm run check:content`: `astro sync`, catches frontmatter YAML breakage.
- `npm run build`: must build clean.
- **Sources check (by hand/agent):** every stat in the draft has a linked
  primary source, verified this cycle. List them in the PR body.
- **Voice check (by hand/agent):** reread for the AI tells above; rough up any
  frictionless symmetry.

## Cadence and scale

- **Query universe:** grow toward ~1,000 distinct queries (breadth is the good
  kind of scale). Mine "people also ask", related searches, and the digest's
  gaps. Add to `scratch/aeo/queries.txt` under the right cluster header.
- **Measurement:** full sweep monthly, watchlist weekly. NOT daily. Re-asking
  the same question daily costs a lot and tells you nothing; rankings move on
  the timescale of publishing, not hours.
- **Publishing:** the amplifier. Target a steady cadence (e.g. 2 guides + 1
  blog post per week) of verified, gap-driven pages. Volume of *distinct,
  verified* pages is what compounds, not measurement frequency.

## How a cycle is triggered

The drafting step needs research + judgment, so it runs as an agent invocation,
not a pure script. Options (pick one when wiring it up):

- **Founder-run cadence:** the simplest. Each cycle is an agent session pointed
  at this runbook + the latest `digest.py` output; it drafts and opens the PR.
- **Local schedule:** a launchd/cron job like the AEO pulse, invoking a headless
  agent with this runbook. Keys and repo are already local. More autonomous;
  keep the PR gate so nothing publishes unread.
- **Cloud routine:** a scheduled cloud agent. Note the probe half can't run in
  the cloud (keys are local), but the drafting half can pull the committed
  digest and open PRs.

Whatever the trigger, the shape is fixed: **draft → gate → PR → the founder
merges.**
