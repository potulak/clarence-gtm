// POST /api/survey: what the problem actually is, in their words.
//
// Written as they go, not on submit. Someone who answers three questions and
// closes the tab has still told us something real, and a form that only
// records finishers throws that away along with the most useful signal it
// has: exactly where people stop.
//
// That last point used to be half true. A save with nothing in it was turned
// away, so someone who pressed Start, read a few questions and left wrote no
// row at all, and the drop-off numbers could not tell "left at question one"
// apart from "read all fifteen and answered none". `reached` carries how far
// they got and a sitting with no answers is now worth a row on its own.
//
// The browser generates a response_id and sends it with every save, so one
// person is one row no matter how many times they come back. Saves are
// upserts that only ever fill fields, never blank them, which means a beacon
// fired on page-hide can never wipe what was already stored.
//
// Backed by the same D1 binding (WAITLIST_DB) as the waitlist, its own table.
// The attribution classifier is imported rather than copied so the two
// endpoints cannot drift on what "social:reddit" means.

import { classifySource } from './waitlist.js';

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

// The ten answers, in the order they are asked.
const ANSWER_FIELDS = [
  'who',
  'who_what',
  'how_long',
  'how_faded',
  'stops',
  'what_say',
  'what_say_what',
  'how_many',
  'tried',
  'tried_what',
  'last_reached',
  'how_goes',
  'mac_messages',
  'access',
  'drafting',
  'blockers',
  'would_use',
  'anything',
  'talk',
];

const MAX_FIELD = 300;
const MAX_ANSWER = 2000; // people type paragraphs about this, and should

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

const clean = (value) =>
  typeof value === 'string' && value.trim() ? value.trim().slice(0, MAX_FIELD) : null;

const answer = (value) =>
  typeof value === 'string' && value.trim() ? value.trim().slice(0, MAX_ANSWER) : null;

export async function onRequestPost({ request, env }) {
  const contentType = request.headers.get('Content-Type') || '';
  const isForm =
    contentType.includes('application/x-www-form-urlencoded') ||
    contentType.includes('multipart/form-data');

  let data;
  try {
    data = isForm ? Object.fromEntries(await request.formData()) : await request.json();
  } catch {
    return json({ ok: false, error: 'unreadable body' }, 400);
  }

  // Honeypot, same as the waitlist: humans leave it empty.
  if (clean(data.website)) {
    return isForm ? seeOther(request) : json({ ok: true });
  }

  // The browser's id for this sitting. Without one there is nothing to upsert
  // onto, and a no-JS submit still needs a row, so fall back to a fresh id.
  const responseId = clean(data.response_id) || crypto.randomUUID();

  const answers = ANSWER_FIELDS.map((f) => answer(data[f]));
  const filled = answers.filter(Boolean).length;

  const raw = typeof data.email === 'string' ? data.email.trim().toLowerCase() : '';
  // Email is optional and only lightly checked: a typo must not cost us the
  // rest of the answers, so a bad address is dropped rather than rejected.
  const email = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(raw) && raw.length <= 254 ? raw : null;

  // How far they got, as a question number. Clamped rather than trusted: this
  // arrives from the page and is the one number here a junk body could set to
  // anything.
  const asked = Number(data.reached);
  const reached = Number.isFinite(asked) ? Math.min(Math.max(Math.trunc(asked), 0), 99) : 0;

  // Still a floor, just a lower one. Something has to say a person was here,
  // otherwise an empty POST writes a row.
  if (!filled && !email && reached < 1) {
    return json({ ok: false, error: 'nothing to record' }, 400);
  }

  const completed = data.completed ? 1 : 0;
  const utm = UTM_FIELDS.map((f) => clean(data[f]));
  const source = classifySource(clean(data.referrer), clean(data.utm_source));

  // COALESCE(excluded.x, x) fills a field and never blanks one, so a later
  // partial save cannot erase an earlier answer. answered/completed only ever
  // go up, for the same reason.
  const setList = [
    ...ANSWER_FIELDS.map((f) => `${f} = COALESCE(excluded.${f}, ${f})`),
    'email = COALESCE(excluded.email, email)',
    'answered = MAX(excluded.answered, answered)',
    'completed = MAX(excluded.completed, completed)',
    'reached = MAX(excluded.reached, reached)',
    "updated_at = datetime('now')",
  ].join(',\n       ');

  await env.WAITLIST_DB.prepare(
    `INSERT INTO survey (response_id, ${ANSWER_FIELDS.join(', ')}, email,
                         answered, completed, reached, source, ${UTM_FIELDS.join(', ')})
     VALUES (?, ${ANSWER_FIELDS.map(() => '?').join(', ')}, ?, ?, ?, ?, ?,
             ${UTM_FIELDS.map(() => '?').join(', ')})
     ON CONFLICT (response_id) DO UPDATE SET
       ${setList}`
  )
    .bind(responseId, ...answers, email, filled, completed, reached, source, ...utm)
    .run();

  return isForm ? seeOther(request) : json({ ok: true, response_id: responseId });
}

// No-JS fallback: back to the page with the thanks state showing.
function seeOther(request) {
  const url = new URL(request.url);
  return new Response(null, {
    status: 303,
    headers: { Location: `${url.origin}/survey/thanks` },
  });
}
