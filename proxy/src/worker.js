// Clarence draft proxy (ADR-0017 / ADR-0025). A thin forwarder + metered
// gateway so the real Anthropic key lives ONLY here, as a Worker secret, never
// on a user's Mac. Each per-install token carries its own credit balance you
// can top up on demand; the proxy prices every request from its token usage and
// stops a token once it has spent its allotment.
//
// The app (mac/src/ai/cloud.ts) points the Anthropic SDK's baseURL at this
// Worker and sends its per-install token in the x-api-key header (that's where
// the SDK puts its "apiKey"). We authorize the token by its credit balance,
// forward to Anthropic with the real key, then charge the measured cost back.
//
// A leaked token can only draft through this proxy until its credit runs out or
// it is rotated; the Anthropic key itself is never exposed, and the account's
// own spend cap bounds the absolute worst case.
//
// Privacy: request/response bodies are forwarded, never logged. The model gets
// no tools; its output is display-only text (threat-model rule 4).
//
// Bindings:
//   ANTHROPIC_API_KEY  (secret) the real key, from a spend-capped workspace
//   ADMIN_KEY          (secret) protects the /admin/* credit endpoints
//   CREDITS            (KV)     per-token budget + spent, in USD
//
// KV layout, per install token:
//   budget:<token>  total USD allotted (raised by /admin/credit)
//   spent:<token>   USD spent so far (incremented after each call)
// A token is authorized only while budget - spent > 0. Adding credit both
// funds AND authorizes a token, so there is one mechanism, not two.

const ANTHROPIC = 'https://api.anthropic.com'

// Opus 4.8 pricing, USD per token (claude-api skill, 2026-07: $5 / $25 per 1M).
const IN_PER_TOK = 5.0 / 1_000_000
const OUT_PER_TOK = 25.0 / 1_000_000
// Cache reads bill ~0.1x input, cache writes ~1.25x input (prompt-caching docs).
const CACHE_READ_PER_TOK = IN_PER_TOK * 0.1
const CACHE_WRITE_PER_TOK = IN_PER_TOK * 1.25

export default {
  async fetch(request, env) {
    const url = new URL(request.url)

    if (request.method === 'POST' && url.pathname.startsWith('/admin/')) {
      return admin(request, env, url)
    }
    if (request.method !== 'POST' || !url.pathname.startsWith('/v1/')) {
      return json({ error: 'not found' }, 404)
    }
    if (!env.ANTHROPIC_API_KEY) return json({ error: 'proxy misconfigured' }, 500)

    // The SDK sends the token as x-api-key (its "apiKey").
    const token = request.headers.get('x-api-key') || ''
    if (!token) return json({ error: 'unauthorized' }, 401)

    const { budget, spent } = await balance(env, token)
    if (budget <= 0) return json({ error: 'unauthorized' }, 401)
    if (spent >= budget) {
      // Out of credits: the app falls back to on-device drafting on any non-200.
      return json({ error: 'credit exhausted', remaining: 0 }, 402)
    }

    // Rebuild headers: only what Anthropic needs, real key swapped in.
    const headers = new Headers()
    headers.set('content-type', 'application/json')
    headers.set('anthropic-version', request.headers.get('anthropic-version') || '2023-06-01')
    headers.set('x-api-key', env.ANTHROPIC_API_KEY)
    const beta = request.headers.get('anthropic-beta')
    if (beta) headers.set('anthropic-beta', beta)

    const body = await request.text() // forwarded verbatim, never logged

    let upstream, text
    try {
      upstream = await fetch(ANTHROPIC + url.pathname + url.search, {
        method: 'POST',
        headers,
        body,
      })
      text = await upstream.text()
    } catch {
      return json({ error: 'upstream unreachable' }, 502)
    }

    // Charge the measured cost only on a successful, usage-bearing response.
    if (upstream.ok) {
      const cost = costOf(text)
      if (cost > 0) {
        // Read-modify-write. KV is eventually consistent; at one user's draft
        // rate races are effectively impossible, and any drift is cents.
        await env.CREDITS.put('spent:' + token, String(spent + cost))
      }
    }

    return new Response(text, {
      status: upstream.status,
      headers: { 'content-type': upstream.headers.get('content-type') || 'application/json' },
    })
  },
}

/** Sum a request's USD cost from the Anthropic response body's usage block. */
function costOf(text) {
  let u
  try {
    u = JSON.parse(text).usage
  } catch {
    return 0
  }
  if (!u) return 0
  return (
    (u.input_tokens || 0) * IN_PER_TOK +
    (u.output_tokens || 0) * OUT_PER_TOK +
    (u.cache_read_input_tokens || 0) * CACHE_READ_PER_TOK +
    (u.cache_creation_input_tokens || 0) * CACHE_WRITE_PER_TOK
  )
}

async function balance(env, token) {
  const [b, s] = await Promise.all([
    env.CREDITS.get('budget:' + token),
    env.CREDITS.get('spent:' + token),
  ])
  return { budget: Number(b) || 0, spent: Number(s) || 0 }
}

/** /admin/credit {token, amount} raises a token's budget (adds credits).
 *  /admin/balance {token} reports budget / spent / remaining.
 *  Both require the x-admin-key header. */
async function admin(request, env, url) {
  if (!env.ADMIN_KEY || request.headers.get('x-admin-key') !== env.ADMIN_KEY) {
    return json({ error: 'unauthorized' }, 401)
  }
  let payload
  try {
    payload = await request.json()
  } catch {
    return json({ error: 'bad request' }, 400)
  }
  const token = String(payload.token || '')
  if (!token) return json({ error: 'token required' }, 400)

  const { budget, spent } = await balance(env, token)

  if (url.pathname === '/admin/balance') {
    return json({ token, budget, spent, remaining: Math.max(0, budget - spent) })
  }
  if (url.pathname === '/admin/credit') {
    const amount = Number(payload.amount)
    if (!Number.isFinite(amount) || amount <= 0 || amount > 1000) {
      return json({ error: 'amount must be between 0 and 1000' }, 400)
    }
    const next = budget + amount
    await env.CREDITS.put('budget:' + token, String(next))
    return json({ token, budget: next, spent, remaining: Math.max(0, next - spent) })
  }
  return json({ error: 'not found' }, 404)
}

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}
