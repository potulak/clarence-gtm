// POST /api/waitlist: the one dynamic endpoint on the site.
// A Cloudflare Pages Function backed by the D1 binding WAITLIST_DB
// (wrangler.toml). Accepts the site form two ways:
//   - fetch with JSON (the normal path, attribution included)
//   - a plain form-encoded POST (the no-JS fallback) -> 303 back to the page
//
// Stores email + first-touch attribution, plus the optional step-two answer
// (name + why keeping in touch matters; decides beta-invite order). The
// same endpoint serves both steps: the insert is an upsert that only ever
// fills name/why on an existing row, so attribution stays first-touch and
// the response never reveals membership.

const UTM_FIELDS = [
  'page',
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'utm_term',
  'utm_content',
  'referrer',
  'landing',
];

const MAX_FIELD = 300;
const MAX_WHY = 1000; // the step-two answer gets room to be a real paragraph

// ---- source classification -------------------------------------------------
// Bucket the first-touch origin so we can answer "did this signup come from an
// AI answer?". We classify from the client-captured first-touch referrer
// (document.referrer on landing, stored in localStorage) and any utm_source
// tag. The POST's own Referer header is same-origin and useless here.
//
// Honest limits, by design: AI-engine referrers are improving but incomplete.
// Google's AI Overviews mostly report as plain google.com (search bucket), and
// a share of AI traffic arrives with no referrer at all and reads as "direct".
// This is directional, not a perfect ledger. Order matters: AI patterns are
// checked before search/social so gemini.google.com beats "google." and
// bing.com/chat (Copilot) beats bing.com.
const AI_PATTERNS = [
  ['chatgpt.com', 'chatgpt'],
  ['chat.openai.com', 'chatgpt'],
  ['oai.', 'chatgpt'],
  ['perplexity.ai', 'perplexity'],
  ['gemini.google.', 'gemini'],
  ['bard.google.', 'gemini'],
  ['claude.ai', 'claude'],
  ['copilot.microsoft.com', 'copilot'],
  ['bing.com/chat', 'copilot'],
  ['edgeservices.bing.com', 'copilot'],
  ['you.com', 'you'],
  ['poe.com', 'poe'],
  ['phind.com', 'phind'],
];
const SEARCH_PATTERNS = [
  ['google.', 'google'],
  ['bing.com', 'bing'],
  ['duckduckgo.com', 'duckduckgo'],
  ['search.brave.com', 'brave'],
  ['ecosia.org', 'ecosia'],
  ['search.yahoo.com', 'yahoo'],
];
const SOCIAL_PATTERNS = [
  ['reddit.com', 'reddit'],
  ['x.com', 'x'],
  ['twitter.com', 'x'],
  ['t.co', 'x'],
  ['news.ycombinator.com', 'hackernews'],
  ['producthunt.com', 'producthunt'],
  ['facebook.com', 'facebook'],
  ['instagram.com', 'instagram'],
  ['linkedin.com', 'linkedin'],
  ['lnkd.in', 'linkedin'],
  ['youtube.com', 'youtube'],
];

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

// Returns a short bucket like "ai:chatgpt", "search:google", "social:reddit",
// "utm:<source>", "other:<host>", or "direct". Exported for unit tests; Pages
// Functions only dispatch on the onRequest* exports, so this is inert at runtime.
export function classifySource(referrer, utmSource) {
  const ref = (referrer || '').toLowerCase();
  for (const [pat, name] of AI_PATTERNS) if (ref.includes(pat)) return `ai:${name}`;
  for (const [pat, name] of SEARCH_PATTERNS) if (ref.includes(pat)) return `search:${name}`;
  for (const [pat, name] of SOCIAL_PATTERNS) if (ref.includes(pat)) return `social:${name}`;
  if (utmSource) return `utm:${utmSource.toLowerCase().slice(0, 40)}`;
  const host = hostOf(ref);
  if (host) return `other:${host}`;
  return 'direct';
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

const clean = (value) =>
  typeof value === 'string' && value.trim() ? value.trim().slice(0, MAX_FIELD) : null;

export async function onRequestPost({ request, env }) {
  const contentType = request.headers.get('Content-Type') || '';
  const isForm =
    contentType.includes('application/x-www-form-urlencoded') ||
    contentType.includes('multipart/form-data');

  let data;
  try {
    if (isForm) {
      data = Object.fromEntries(await request.formData());
    } else {
      data = await request.json();
    }
  } catch {
    return json({ ok: false, error: 'unreadable body' }, 400);
  }

  // Honeypot: the visually-hidden "website" field is empty for humans.
  // Bots that fill it get a cheerful 200 and no row.
  if (clean(data.website)) {
    return isForm ? seeOther(request) : json({ ok: true });
  }

  const email = typeof data.email === 'string' ? data.email.trim().toLowerCase() : '';
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || email.length > 254) {
    return json({ ok: false, error: 'invalid email' }, 400);
  }

  const values = UTM_FIELDS.map((f) => clean(data[f]));
  const name = clean(data.name);
  const why =
    typeof data.why === 'string' && data.why.trim()
      ? data.why.trim().slice(0, MAX_WHY)
      : null;

  // First-touch source bucket. Set on insert only, so it stays first-touch
  // (the ON CONFLICT update never touches it), same as the UTM columns.
  const source = classifySource(clean(data.referrer), clean(data.utm_source));

  await env.WAITLIST_DB.prepare(
    `INSERT INTO waitlist (email, name, why, source, ${UTM_FIELDS.join(', ')})
     VALUES (?, ?, ?, ?, ${UTM_FIELDS.map(() => '?').join(', ')})
     ON CONFLICT (email) DO UPDATE SET
       name = COALESCE(excluded.name, name),
       why = COALESCE(excluded.why, why)`
  )
    .bind(email, name, why, source, ...values)
    .run();

  return isForm ? seeOther(request) : json({ ok: true });
}

// No-JS fallback: bounce back to the homepage waitlist with the joined flag.
function seeOther(request) {
  const url = new URL(request.url);
  return new Response(null, {
    status: 303,
    headers: { Location: `${url.origin}/?joined=1#waitlist` },
  });
}
