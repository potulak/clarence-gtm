// Clarence Reddit cockpit, cloud half.
//
// What this is for: reading and triaging the queue from a phone when both
// laptops are shut. It deliberately does NOT collect. Reddit login-walls
// logged-out reads, and the fix for that is an authenticated session, which is
// the last thing that should ever run from a datacenter. Collection stays on the
// Mac. This serves what the Mac last computed, and records what you decide.
//
// The invariant, carried over from the two-laptop design: the Mac owns posts and
// verdicts, this owns your decisions. Neither writes the other's columns.

const JSON_HEADERS = { "Content-Type": "application/json; charset=utf-8" };

function json(obj, status = 200, extra = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...JSON_HEADERS, ...extra },
  });
}

// Length-independent compare, so the key cannot be recovered a byte at a time.
// Cheap here, and the alternative is a habit that stops being cheap elsewhere.
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const enc = new TextEncoder();
  const x = enc.encode(a);
  const y = enc.encode(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function presentedKey(request, url) {
  const q = url.searchParams.get("k");
  if (q) return q;
  const cookie = request.headers.get("Cookie") || "";
  // Parse properly rather than substring-matching the header, so a cookie whose
  // VALUE happens to contain "ck=<key>" cannot be used to fake one.
  for (const part of cookie.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === "ck") return rest.join("=");
  }
  return null;
}

// The status of a post is the latest action recorded for it. Same last-write-wins
// replay the Mac does, so both sides agree without either having to be told.
const LATEST_STATUS = `
  SELECT a.post_id, a.status, a.reason, a.note
  FROM actions a
  JOIN (SELECT post_id, MAX(at) AS at FROM actions GROUP BY post_id) m
    ON m.post_id = a.post_id AND m.at = a.at
`;

async function handleQueue(env) {
  const [rows, acts, meta, reps] = await Promise.all([
    env.DB.prepare("SELECT * FROM queue ORDER BY rank ASC").all(),
    env.DB.prepare(LATEST_STATUS).all(),
    env.DB.prepare("SELECT k, v FROM meta").all(),
    // Newest first, and NOT joined to the queue: a thread you already posted in
    // has left the queue, and those are precisely the ones that get replies.
    env.DB.prepare("SELECT * FROM replies ORDER BY created_at DESC LIMIT 100").all(),
  ]);

  const status = new Map();
  for (const a of acts.results || []) status.set(a.post_id, a);

  const threads = (rows.results || []).map((r) => {
    const a = status.get(r.id);
    return {
      ...r,
      is_new: !!r.is_new,
      stale: !!r.stale,
      status: a ? a.status : "todo",
      status_reason: a ? a.reason || "" : "",
      status_note: a ? a.note || "" : "",
      // The phone cannot draft: drafting spends Anthropic credits and that key
      // is not here. The desktop board still does it.
      draft: "",
      post_text: "",
      angle: "",
    };
  });

  const m = Object.fromEntries((meta.results || []).map((r) => [r.k, r.v]));
  const live = threads.filter((t) => t.status === "todo");

  return json({
    generated: m.generated || null,
    pushed_at: m.pushed_at || null,
    source: "cloud",
    // Counted on the Mac, carried up by the push, because the table behind it
    // never leaves that machine.
    voice: m.voice ? JSON.parse(m.voice) : null,
    count: live.length,
    total: threads.length,
    threads,
    replies: reps.results || [],
    // Honest staleness, rather than a board that looks live when nothing has
    // pushed for two days because both laptops were shut.
    health: {
      pushed_at: m.pushed_at || null,
      generated: m.generated || null,
      note: "served from D1. Collection happens on the Mac, not here.",
    },
  });
}

// ===========================================================================
// The real board, added 2026-10-17.
//
// Up to tonight this Worker served a stripped view: the whole queue as a flat
// list, with no drafting beyond one button and no notion of the day's shortlist,
// the people worth going back to or the communities worth breaking into. The Mac
// board had all of that, so the two phone views forked and the good one was only
// reachable over plain HTTP on the home network, which is how an evening got
// spent fighting Chrome's HTTPS upgrade instead of talking to anyone.
//
// What follows is the Mac board's own API, served from D1, so that ONE m.html
// works against either server and the fork is closed.
//
// What is NOT here, and why:
//
//   /api/reddit/full   reads a thread live. That is dead everywhere as of
//                      tonight, not just at the edge: measured from the
//                      founder's own home connection, the per-thread Atom feed
//                      answers 403 and so does the .json, while old.reddit
//                      answers the login page. So this returns an honest
//                      failure and the page falls back to the stored body and
//                      the paste box. `bodies.py` now fills that stored body in
//                      from the one feed that does still answer.
//
//   /api/reddit/transcribe  needs a speech model. The proxy in front of the
//                      Anthropic key speaks /v1/messages and nothing else, and
//                      Claude has no audio input, so there is no path from here
//                      to a transcript. Rather than ship a record button that
//                      can only fail, the board is TOLD there is no transcriber
//                      (`can_transcribe: false` below) and leaves the control
//                      out. The keyboard's own dictation key still works and is
//                      on-device anyway, which is what the LAN board already
//                      falls back to.

// Tables the push replaces wholesale. Created here rather than assumed, because
// a Worker deployed before the matching `wrangler d1 execute` would otherwise
// 500 on every read with "no such table" and look like a broken board rather
// than an unmigrated one.
const DDL = [
  // The four that predate the real board. Listed here too, so a database
  // rebuilt from nothing comes up working rather than 500ing on `actions`.
  // `queue` and `/api/reddit` are the staleness watchdog's contract.
  `CREATE TABLE IF NOT EXISTS queue (
     id TEXT PRIMARY KEY, rank INTEGER, lane TEXT, subreddit TEXT, title TEXT,
     url TEXT, snippet TEXT, author TEXT, age_hours REAL, num_comments INTEGER,
     score INTEGER, fit REAL, llm_score REAL, why TEXT, post_rule TEXT,
     matched_cluster TEXT, matched_phrase TEXT, found_via TEXT, source TEXT,
     first_seen TEXT, is_new INTEGER, stale INTEGER,
     archived INTEGER, locked INTEGER)`,
  `CREATE INDEX IF NOT EXISTS queue_rank ON queue(rank)`,
  `CREATE TABLE IF NOT EXISTS actions (
     at TEXT NOT NULL, post_id TEXT NOT NULL, status TEXT, reason TEXT,
     note TEXT, origin TEXT)`,
  `CREATE INDEX IF NOT EXISTS actions_post ON actions(post_id, at)`,
  `CREATE INDEX IF NOT EXISTS actions_at ON actions(at)`,
  `CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)`,
  `CREATE TABLE IF NOT EXISTS replies (
     id TEXT PRIMARY KEY, post_id TEXT, subreddit TEXT, author TEXT, body TEXT,
     url TEXT, created_at TEXT, post_title TEXT, my_comment TEXT,
     post_url TEXT, post_snippet TEXT, asked_at TEXT, ask_outcome TEXT,
     acknowledged INTEGER DEFAULT 0)`,
  `CREATE INDEX IF NOT EXISTS replies_when ON replies(created_at)`,
  `CREATE TABLE IF NOT EXISTS picks (
     id TEXT PRIMARY KEY, rank INTEGER, rank_score REAL, tier TEXT,
     why_picked TEXT, lane TEXT, subreddit TEXT, title TEXT, url TEXT,
     author TEXT, snippet TEXT, post_text TEXT, comments TEXT, age_hours REAL,
     num_comments INTEGER, score INTEGER, score_at TEXT, fit REAL,
     llm_score REAL, why TEXT, post_rule TEXT, post_rule_says TEXT,
     first_seen TEXT, created_utc REAL, prospect INTEGER, prospect_note TEXT,
     asked_at TEXT, status TEXT, draft TEXT, angle TEXT,
     archived INTEGER, locked INTEGER)`,
  `CREATE INDEX IF NOT EXISTS picks_rank ON picks(rank)`,
  `CREATE TABLE IF NOT EXISTS people (
     id TEXT PRIMARY KEY, author TEXT, subreddit TEXT, title TEXT, url TEXT,
     snippet TEXT, why TEXT, status TEXT, prospect_note TEXT, prospect_at TEXT)`,
  `CREATE TABLE IF NOT EXISTS communities (
     subreddit TEXT PRIMARY KEY, n INTEGER, good INTEGER, hit_rate REAL,
     avg_score REAL)`,
  `CREATE TABLE IF NOT EXISTS decisions (
     at TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT, payload TEXT,
     origin TEXT)`,
  `CREATE INDEX IF NOT EXISTS decisions_at ON decisions(at)`,
  `CREATE TABLE IF NOT EXISTS drafts (
     post_id TEXT PRIMARY KEY, draft TEXT, angle TEXT, post_text TEXT, at TEXT)`,
  `CREATE INDEX IF NOT EXISTS drafts_at ON drafts(at)`,
];

// SQLite has no ADD COLUMN IF NOT EXISTS, and D1 has no migration runner wired
// up here, so the shape is brought forward on first use. Cheap: one PRAGMA read,
// and the ALTERs only ever run once.
let schemaReady = false;

async function ensureSchema(env) {
  if (schemaReady) return;
  // The board's own tables. Made here rather than assumed, because a deploy
  // lands before the next hourly push does, and the founder opens the board the
  // moment it deploys. Without this he would get a 500 and "no such table:
  // picks", which reads as a broken board rather than as one waiting for its
  // first push. With it he gets an empty deck that fills within the hour.
  try {
    await env.DB.batch(DDL.map((q) => env.DB.prepare(q)));
  } catch {
    // Lost a race with another request doing the same thing. Harmless.
  }
  const want = {
    replies: {
      my_comment: "TEXT", post_url: "TEXT", post_snippet: "TEXT",
      asked_at: "TEXT", ask_outcome: "TEXT", acknowledged: "INTEGER DEFAULT 0",
    },
    // Can the founder even reply to this one. Added 2026-10-18, when the
    // collector started keeping what the OAuth listing had always told it.
    // No DEFAULT, deliberately: NULL is "nobody said", and a 0 here would be
    // the phone promising you can comment on a thread Reddit has closed.
    queue: { archived: "INTEGER", locked: "INTEGER" },
    picks: { archived: "INTEGER", locked: "INTEGER" },
  };
  for (const [table, cols] of Object.entries(want)) {
    let have;
    try {
      have = await env.DB.prepare(`PRAGMA table_info(${table})`).all();
    } catch {
      continue;                      // no such table yet, the push will make it
    }
    const names = new Set((have.results || []).map((r) => r.name));
    for (const [col, type] of Object.entries(cols)) {
      if (names.has(col)) continue;
      try {
        await env.DB.prepare(
          `ALTER TABLE ${table} ADD COLUMN ${col} ${type}`
        ).run();
      } catch {
        // Lost a race with another request doing the same thing. Harmless.
      }
    }
  }
  schemaReady = true;
}

const PICK_COLS = [
  "id", "rank", "rank_score", "tier", "why_picked", "lane", "subreddit",
  "title", "url", "author", "snippet", "post_text", "comments", "age_hours",
  "num_comments", "score", "score_at", "fit", "llm_score", "why", "post_rule",
  "post_rule_says", "first_seen", "created_utc", "prospect", "prospect_note",
  "asked_at", "status", "draft", "angle", "archived", "locked",
];

// Three-state, and the reason they are not treated like `is_new` and `stale`
// below. Those are yes-or-no and a missing one is a no; these have a third
// state that has to survive the trip, because the phone reads a null as "worth
// trying" and a 0 as "Reddit says you can reply". D1 binds numbers, not
// booleans, so a real true has to become a 1 on the way in.
function threeState(v) {
  return v === undefined || v === null ? null : (v ? 1 : 0);
}

const PEOPLE_COLS = [
  "id", "author", "subreddit", "title", "url", "snippet", "why", "status",
  "prospect_note", "prospect_at",
];

const REPLY_COLS = [
  "id", "post_id", "subreddit", "author", "body", "url", "created_at",
  "post_title", "my_comment", "post_url", "post_snippet", "asked_at",
  "ask_outcome", "acknowledged",
];

// What the page asks for on load, and the only read it needs to draw the deck.
//
// Shaped to match the Mac's `/api/reddit/shortlist` field for field, so the page
// does not have to know which server answered. Where this side genuinely cannot
// supply something, it says so in the payload rather than leaving the field out:
// `can_transcribe` is the one that changes what the page draws.
async function handleShortlist(env, url) {
  const want = Math.max(1, Math.min(60, parseInt(url.searchParams.get("want") || "20", 10) || 20));

  const [rows, acts, dec, drafts, meta, reps, folk, comm] = await Promise.all([
    env.DB.prepare("SELECT * FROM picks ORDER BY rank ASC LIMIT ?").bind(want).all(),
    env.DB.prepare(LATEST_STATUS).all(),
    env.DB.prepare(
      "SELECT at, kind, subject, payload FROM decisions ORDER BY at ASC"
    ).all(),
    env.DB.prepare("SELECT * FROM drafts").all(),
    env.DB.prepare("SELECT k, v FROM meta").all(),
    env.DB.prepare(
      "SELECT * FROM replies WHERE COALESCE(acknowledged, 0) = 0 " +
      "ORDER BY created_at DESC LIMIT 100"
    ).all(),
    env.DB.prepare("SELECT * FROM people ORDER BY prospect_at DESC").all(),
    env.DB.prepare(
      "SELECT * FROM communities ORDER BY (good >= 2) DESC, hit_rate DESC, good DESC LIMIT 8"
    ).all(),
  ]);

  const m = Object.fromEntries((meta.results || []).map((r) => [r.k, r.v]));
  const status = new Map();
  for (const a of acts.results || []) status.set(a.post_id, a);
  const draft = new Map();
  for (const d of drafts.results || []) draft.set(d.post_id, d);

  // Decisions taken here since the last push. The Mac has not seen them yet, so
  // replaying them over the top is what stops the phone showing you a person you
  // already asked or a reply you already cleared.
  const saved = new Map();          // post id -> saved or not
  const asked = new Map();          // reddit handle -> {at, outcome}
  const acked = new Set();          // reply ids dealt with
  for (const d of dec.results || []) {
    let p = {};
    try { p = JSON.parse(d.payload || "{}"); } catch {}
    const who = (d.subject || "").toLowerCase();
    if (d.kind === "prospect") saved.set(d.subject, { on: !!p.on, note: p.note || "" });
    else if (d.kind === "ask_sent") asked.set(who, { at: d.at, outcome: "sent" });
    else if (d.kind === "ask_outcome") {
      const was = asked.get(who) || { at: d.at };
      asked.set(who, { at: was.at, outcome: p.outcome || "sent" });
    } else if (d.kind === "reply_ack") {
      if (p.on === false) acked.delete(d.subject);
      else acked.add(d.subject);
    }
  }

  const picks = [];
  (rows.results || []).forEach((r) => {
    const a = status.get(r.id);
    const st = a ? a.status : r.status || "todo";
    // Triaged from the phone since the last push: it is off the day's list, the
    // same as it would be on the laptop. Without this, every card you skipped on
    // the train comes back the moment you reopen the board.
    if (a && ["skip", "posted", "done"].includes(a.status)) return;
    const d = draft.get(r.id) || {};
    const sv = saved.get(r.id);
    let comments = [];
    try { comments = JSON.parse(r.comments || "[]"); } catch {}
    const who = (r.author || "").toLowerCase();
    const ask = asked.get(who);
    picks.push({
      ...r,
      comments,
      status: st,
      prospect: sv ? sv.on : !!r.prospect,
      prospect_note: (sv && sv.note) || r.prospect_note || "",
      asked_at: ask ? ask.at : r.asked_at || null,
      // The founder's own work in progress wins over whatever the push carried:
      // he typed it more recently than the Mac computed the list.
      draft: d.draft != null ? d.draft : r.draft || "",
      angle: d.angle != null ? d.angle : r.angle || "",
      post_text: d.post_text ? d.post_text : r.post_text || "",
      fit: r.fit != null ? r.fit : r.rank_score,
    });
  });

  const replies = (reps.results || [])
    .filter((r) => !acked.has(r.id))
    .map((r) => {
      const ask = asked.get((r.author || "").toLowerCase());
      return {
        ...r,
        asked_at: ask ? ask.at : r.asked_at || null,
        ask_outcome: ask ? ask.outcome : r.ask_outcome || null,
      };
    });

  const jsonMeta = (k, fallback) => {
    try { return JSON.parse(m[k]); } catch { return fallback; }
  };
  const counts = jsonMeta("counts", {});
  // `shown` describes the list the Mac built. What the phone is actually about
  // to draw is shorter, because of everything triaged here since. Say the true
  // number, or the header and the deck disagree with each other.
  if (counts && counts.shown != null) counts.shown = picks.length;

  return json({
    picks,
    counts,
    // The phone draws its deck from THIS call, not /api/reddit, so the voice
    // count has to ride along here or it never reaches the screen it is for.
    voice: jsonMeta("voice", null),
    new_communities: (comm.results || []).map((c) => ({ ...c })),
    replies,
    people: (folk.results || []).map((p) => ({ ...p })),
    asks: jsonMeta("asks", {}),
    bar: jsonMeta("bar", {}),
    window_hours: parseInt(m.window_hours || "168", 10),
    // Whether the day's list had to reach past a day to fill itself. A list
    // of stale posts looks exactly like a fresh one until something says so.
    widened: m.widened === "True" || m.widened === "true",
    freshest_hours: m.freshest_hours ? parseFloat(m.freshest_hours) : null,
    health: {
      ...jsonMeta("health", {}),
      pushed_at: m.pushed_at || null,
      source: "cloud",
      note: "served from D1. Collection happens on the Mac, not here.",
    },
    // The two things the page must not guess about this server.
    can_transcribe: false,
    can_fetch_threads: false,
    source: "cloud",
  });
}

// The saved-people tab. Its own endpoint because the page loads it lazily, and
// because the Mac's board has always had one.
async function handleProspects(env) {
  const [folk, dec] = await Promise.all([
    env.DB.prepare("SELECT * FROM people ORDER BY prospect_at DESC").all(),
    env.DB.prepare(
      "SELECT at, subject, payload FROM decisions WHERE kind = 'prospect' ORDER BY at ASC"
    ).all(),
  ]);
  const out = new Map();
  for (const p of folk.results || []) out.set(p.id, { ...p });
  // Someone saved here since the last push is not in `people` yet, because that
  // table is a projection of the Mac's. Fill them in from the pick they were
  // saved on, so the tab shows them straight away instead of after the next
  // hourly sync.
  for (const d of dec.results || []) {
    let p = {};
    try { p = JSON.parse(d.payload || "{}"); } catch {}
    if (!p.on) { out.delete(d.subject); continue; }
    if (out.has(d.subject)) {
      if (p.note) out.get(d.subject).prospect_note = p.note;
      continue;
    }
    const row = await env.DB.prepare(
      "SELECT id, author, subreddit, title, url, snippet, why, status FROM picks WHERE id = ?"
    ).bind(d.subject).first();
    if (row) out.set(d.subject, { ...row, prospect_note: p.note || "", prospect_at: d.at });
  }
  const people = [...out.values()].sort(
    (a, b) => String(b.prospect_at || "").localeCompare(String(a.prospect_at || ""))
  );
  return json({ count: people.length, people });
}

// One call through the proxy, with the two money failures kept apart.
//
// 402 from the PROXY  = this phone token's budget is spent (raise it in KV)
// 400 from ANTHROPIC  = the account is out of credit (top up billing)
//
// An earlier version collapsed both into "draft failed upstream (400)" and sent
// a debugging session after the wrong thing entirely.
async function askModel(env, { system, user, maxTokens = 900 }) {
  if (!env.PROXY_TOKEN || !env.PROXY) {
    return { ok: false, error: "the draft proxy is not configured" };
  }
  const upstream = await env.PROXY.fetch("https://proxy/v1/messages", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "anthropic-version": "2023-06-01",
      "x-api-key": env.PROXY_TOKEN.trim(),
    },
    body: JSON.stringify({
      model: "claude-sonnet-4-5-20250929",
      max_tokens: maxTokens,
      system,
      messages: [{ role: "user", content: user }],
    }),
  });
  const text = await upstream.text();
  if (!upstream.ok) {
    if (upstream.status === 402) {
      return { ok: false, error: "This phone's drafting budget is used up. Raise it in the proxy KV." };
    }
    let detail = "";
    try { detail = (JSON.parse(text).error || {}).message || ""; } catch {}
    return {
      ok: false,
      error: detail ? `Anthropic said: ${detail.slice(0, 200)}`
                    : `the model call failed upstream (${upstream.status})`,
    };
  }
  try {
    const d = JSON.parse(text);
    const out = (d.content || []).map((b) => b.text || "").join("").trim();
    // Never ship an em dash. The same two substitutions enrich.py makes, for the
    // same reason: a dash in a drafted comment is the tell the founder will not
    // have, and the prompt asking for none of them is not a guarantee.
    // Written as escapes, not as the character, so this file stays clean under
    // the repo's own em dash check.
    return { ok: true, text: out.replace(/ \u2014 /g, ". ")
                               .replace(/\u2014/g, ", ") };
  } catch {
    return { ok: false, error: "could not read the model's reply" };
  }
}

// The persona is resolved on the Mac and pushed up, so there is still exactly one
// place a voice is edited. Two voices travel, because the board offers both.
function personaFor(meta, authorId) {
  return (
    meta[`persona_${authorId}`] ||
    meta.persona_system ||
    "Write a warm, honest, first-person Reddit comment. Plain, human, specific. " +
      "No marketing voice, no sign-off, never an em dash."
  );
}

async function metaMap(env) {
  const r = await env.DB.prepare("SELECT k, v FROM meta").all();
  return Object.fromEntries((r.results || []).map((x) => [x.k, x.v]));
}

// Drafting. The angle is the SPINE of the draft, not a hint, which is the same
// contract `draft_reply` states in enrich.py and the only reason doing this from
// a phone is worth anything: the part only he can supply is what he wants to say.
const CONTEXTS = {
  new_comment: "Write a NEW top-level comment responding to the original post.",
  reply_comment: "REPLY to the one specific comment shown below, in context.",
  reply_to_reply:
    "REPLY to the person who answered YOUR comment. They are already in " +
    "conversation with you, so there is nothing to establish: no introduction, " +
    "no restating your original point, no mention of what you are building " +
    "unless they asked. Answer what they actually said and nothing else.",
};

const LENGTHS = {
  default:
    "LENGTH: keep it short, the length of a real Reddit comment. Two to four " +
    "sentences, one idea. Do NOT write an essay or stack up paragraphs of " +
    "advice. Do NOT play therapist, diagnose the person, or tell them to seek " +
    "professional help unless they explicitly asked. Relate to them first, then " +
    "at most one small suggestion or a real question. Match the energy and " +
    "length of the thread.",
  reply_to_reply:
    "LENGTH: very short. One to three sentences. This is the second turn of a " +
    "conversation that is already going, so it should read like a person typing " +
    "back on their phone. No summary of the thread, no fresh advice, no " +
    "sign-off. If a single sentence answers them, send a single sentence.",
};

async function handleDraft(env, body) {
  const id = String(body.id || "");
  const context = CONTEXTS[body.context] ? body.context : "new_comment";
  const angle = String(body.angle || "").trim();
  const meta = await metaMap(env);

  const post = id
    ? await env.DB.prepare(
        "SELECT subreddit, title, snippet, post_text, comments, why, post_rule FROM picks WHERE id = ?"
      ).bind(id).first()
    : null;
  const pasted = String(body.post_text || "").trim();
  const title = (post && post.title) || "";
  const selftext = pasted || (post && (post.post_text || post.snippet)) || "";
  const target = String(body.target_comment || "").trim();

  if (!selftext && !target) {
    return json(
      { ok: false,
        error: "Nothing to write from. Paste the post in, or say what you want to say." },
      400
    );
  }

  let user = `THREAD TITLE: ${title}\n\nORIGINAL POST:\n${selftext}\n\n`;
  if (context === "reply_to_reply" || context === "reply_comment") {
    const mine = String(body.my_comment || "").trim();
    if (context === "reply_to_reply" && mine) {
      user += `WHAT YOU ALREADY SAID IN THIS THREAD, which they are answering:\n${mine}\n\n`;
    }
    if (target) user += `THE COMMENT YOU ARE REPLYING TO:\n${target}\n\n`;
  } else if (post && post.comments) {
    let got = [];
    try { got = JSON.parse(post.comments || "[]"); } catch {}
    if (got.length) {
      user += "SOME COMMENTS FOR CONTEXT:\n" + got.slice(0, 8).join("\n---\n") + "\n\n";
    }
  }
  if (post && post.post_rule) {
    user += `THIS SUBREDDIT'S RULE: ${post.post_rule}\n\n`;
  }
  if (angle) {
    user +=
      "WHAT HE WANTS TO SAY, in his own words. This was dictated, so it may be " +
      "rough, repeat itself, or trail off. It is still the SPINE of the reply, " +
      `not a suggestion:\n${angle}\n\n` +
      "Build the comment around HIS point. Keep his specifics, his examples and " +
      "his opinion, even the blunt ones. Clean up the grammar and the false " +
      "starts, keep his words wherever they already work, and do not smooth him " +
      "into someone more polite than he is. Do not add advice he did not give. " +
      "If what he says does not fully answer the post, that is fine, say his " +
      "piece and stop.\n\n";
  }
  user +=
    CONTEXTS[context] + "\n\n" + (LENGTHS[context] || LENGTHS.default) +
    "\nWrite only the text, ready to paste. No preamble, no quotes around it.";
  // Thin post and nothing dictated is the case where a confident answer is a
  // wrong one, so say so instead of guessing.
  if (!angle && selftext.length < 200 && !target) {
    user +=
      "\n\nThis post is very short and he has not said what he wants to say, so " +
      "if there is not enough here to answer honestly, say exactly that in one " +
      "line instead of inventing a reply.";
  }

  const cap = context === "reply_to_reply" ? 160 : angle ? 320 : 220;
  const got = await askModel(env, {
    system: personaFor(meta, String(body.author || "me-founder")),
    user,
    maxTokens: cap,
  });
  if (!got.ok) return json(got, 502);
  return json({ ok: true, id, draft: got.text, context,
                author: String(body.author || "me-founder") });
}

// One thing changed about a draft, and the rest left alone. Rewriting from
// scratch on every tweak is how you lose the version you nearly liked.
const REVISIONS = {
  shorter: "Cut it down hard. Same point, fewer words. Lose any sentence that is not doing work. If it can be two sentences, make it two.",
  warmer: "Make it sound like a person who has been there, not someone being helpful at them. Less careful, more human. Contractions are fine.",
  "less-advice": "Stop advising. Keep the part where he relates to them, drop the suggestions, or leave at most one. Ending without a fix is fine.",
  "more-me": "Plainer. Take out anything that reads as written rather than said: tidy parallel phrases, wrapped-up endings, any word he would not use out loud. Slightly uneven is better than polished.",
  "less-salesy": "Take the pitch out. He is a person in a thread who happens to have built something, not someone selling it. If the product does not need to be named, do not name it.",
  "no-question": "Drop the question at the end. Let it just be a thing he said.",
};

async function handleRevise(env, body) {
  const draft = String(body.draft || "").trim();
  const asked = String(body.instruction || "").trim();
  if (!draft) return json({ ok: false, error: "nothing to revise yet" }, 400);
  if (!asked) return json({ ok: false, error: "say what to change" }, 400);
  // A preset button and something he typed are two different requests, and
  // treating them as one was the bug. The preset is a surgical edit and "keep
  // everything else" is right for it. Free text is him saying what the draft
  // gets WRONG, and down the surgical path it arrived wrapped in four orders to
  // change nothing else: the only way to obey both "say this as well" and
  // "leave the rest alone" is to bolt a paragraph onto the end, so that is what
  // it did, every time. Free text re-drafts instead.
  const preset = Object.prototype.hasOwnProperty.call(REVISIONS, asked);
  const what = preset ? REVISIONS[asked] : asked;
  const meta = await metaMap(env);

  let user = "";
  if (String(body.post_text || "").trim()) {
    user += `THE POST HE IS REPLYING TO:\n${String(body.post_text).trim().slice(0, 4000)}\n\n`;
  }
  if (String(body.angle || "").trim()) {
    user += `WHAT HE WANTED TO SAY, in his own words:\n${String(body.angle).trim()}\n\n`;
  }
  user += `THE CURRENT DRAFT:\n${draft}\n\n`;
  if (preset) {
    user +=
      `CHANGE THIS, and only this:\n${what}\n\n` +
      "Keep everything else as close to the current draft as you can. This is an " +
      "edit, not a new attempt: do not reach for a different angle, a different " +
      "opening or a different point. Keep his words where they already work.\n";
  } else {
    user +=
      "WHY HE IS REWRITING IT. This is him telling you what the draft gets wrong, " +
      `and it is the most important thing on this page:\n${what}\n\n` +
      "Write the comment AGAIN. Do not edit the draft, and do not keep its shape. " +
      "Go back to the post, to what he wanted to say, and to what he has just told " +
      "you, and write ONE reply that holds all three at once.\n" +
      "What he just said is the point of the comment now, so it belongs in the " +
      "first sentence or two. If it replaces what the draft was arguing, then drop " +
      "what the draft was arguing. NEVER answer this by keeping the draft and " +
      "adding a sentence or a paragraph to the end: that is the exact thing he is " +
      "complaining about.\n" +
      "The draft above is a reference for tone and for any phrase worth keeping. " +
      "It is not a thing to be preserved.\n";
  }
  user +=
    "Still true: no statistics or numbers, no em dashes, no therapy voice, and " +
    "the length of a real Reddit comment.\n" +
    "Write only the revised text, ready to paste. No preamble, no notes on what " +
    "you changed, no quotes around it.";

  const got = await askModel(env, {
    system: personaFor(meta, String(body.author || "me-founder")),
    user,
    maxTokens: 400,
  });
  if (!got.ok) return json(got, 502);
  return json({ ok: true, draft: got.text, applied: asked, rewrote: !preset });
}

// Asking ONE person, who has just been talking with you, to try the thing.
//
// Drafting records nothing. A message he looks at and discards must not count as
// having asked someone, or the "already asked" guard starts hiding people he
// never spoke to.
const ASK_RULES =
  "You are writing a short message asking ONE person, who has just been talking " +
  "with you in a Reddit thread, whether they would try the thing you built and " +
  "tell you what they think.\n\n" +
  "Hard rules, all of them non-negotiable:\n" +
  "- Say that YOU built it. Not \"I found this\", not \"there's an app\". He built " +
  "it, and burying that is the single fastest way to be read as a shill.\n" +
  "- ONE link, at the end. Never two.\n" +
  "- Do not describe it as private, secure, or local. Whatever it does with data " +
  "is not settled yet and a promise made here would be a promise he has to keep.\n" +
  "- No feature list. One sentence on what it does, at most.\n" +
  "- Ask for their reaction, not a signup. The point is to learn, not to convert.\n" +
  "- Short. Three sentences is plenty, two is better. This is a person replying " +
  "on their phone, not a landing page.\n" +
  "- Make it specific to what THEY said. If they described losing touch with " +
  "someone, say that back in their words. A message that would fit anybody reads " +
  "as a template, because it is one.\n" +
  "- No hype words: revolutionary, game-changing, seamless, effortless.\n" +
  "- If asking would be tone deaf given what they wrote, say so instead of " +
  "writing the message. Reply with exactly: SKIP: <one line on why>.\n";

async function handleAsk(env, body) {
  const meta = await metaMap(env);
  const link = meta.survey_url ||
    "https://getclarence.app/survey?utm_source=reddit&utm_medium=ask&utm_campaign=first10";
  const channel = body.channel === "dm" ? "dm" : "thread";
  const where = channel === "thread"
    ? "This is a REPLY IN THE THREAD, public, where the conversation already is."
    : "This is a DIRECT MESSAGE. They have been talking with him, so it " +
      "continues a conversation rather than starting one. Be even shorter and " +
      "more obviously human than a public reply would be.";

  let user = `${ASK_RULES}\n${where}\n\n` +
    `WHAT THEY SAID:\n${String(body.their_words || "").trim().slice(0, 1500)}\n\n`;
  if (String(body.my_comment || "").trim()) {
    user += `WHAT HE SAID TO THEM EARLIER:\n${String(body.my_comment).trim().slice(0, 800)}\n\n`;
  }
  user += `THE LINK: ${link}\n\nWrite only the message, ready to send. No preamble, no quotes.`;

  const got = await askModel(env, {
    system: personaFor(meta, String(body.author || "me-founder")),
    user,
    maxTokens: 220,
  });
  if (!got.ok) return json(got, 502);
  if (got.text.toUpperCase().startsWith("SKIP")) {
    return json({ ok: false, skip: true, reason: got.text.slice(5).replace(/^[:\s]+/, "") });
  }
  return json({ ok: true, text: got.text, channel, url: link });
}

// Everything the founder decides here that is not a post status. Append only,
// same as `actions`, and pulled down by the Mac on the same watermark.
async function recordDecision(env, kind, subject, payload) {
  const at = new Date().toISOString();
  await env.DB.prepare(
    "INSERT INTO decisions (at, kind, subject, payload, origin) VALUES (?, ?, ?, ?, 'phone')"
  ).bind(at, kind, subject || null, JSON.stringify(payload || {})).run();
  return at;
}

// A box on the card, written through on every blur so a dictated angle survives
// the phone locking itself. Status changes still go to `actions`: that is the one
// table whose replay the Mac already understands.
async function handleItem(env, body) {
  const id = String(body.id || "");
  if (!id) return json({ ok: false, error: "id required" }, 400);
  const out = { ok: true, id };

  // Every step of writing one reply, with its words, appended where the Mac can
  // replay it into the voice table. `drafts` cannot carry this: it is one row
  // per post and it overwrites, so it answers "what is in the box" and never
  // "who put it there", which is the only question a voice can be learned from.
  if ("draft" in body && String(body.draft || "").trim()) {
    const src = String(body.draft_src || "edit");
    const step = ["model", "revise", "edit", "final"].includes(src) ? src : "edit";
    await recordDecision(env, "voice", id, {
      step,
      text: String(body.draft),
      author: body.author ? String(body.author) : null,
      instruction: body.instruction ? String(body.instruction).slice(0, 200) : null,
    });
  }
  // What he said out loud, which is his voice with nothing in between.
  if ("angle" in body && String(body.angle || "").trim()) {
    await recordDecision(env, "voice", id, {
      step: "angle",
      text: String(body.angle),
    });
  }

  const fields = ["draft", "angle", "post_text"].filter((f) => f in body);
  if (fields.length) {
    const row = await env.DB.prepare(
      "SELECT draft, angle, post_text FROM drafts WHERE post_id = ?"
    ).bind(id).first();
    const now = {
      draft: row ? row.draft : null,
      angle: row ? row.angle : null,
      post_text: row ? row.post_text : null,
    };
    for (const f of fields) now[f] = String(body[f] ?? "");
    await env.DB.prepare(
      "INSERT INTO drafts (post_id, draft, angle, post_text, at) VALUES (?, ?, ?, ?, ?) " +
      "ON CONFLICT(post_id) DO UPDATE SET draft = excluded.draft, " +
      "angle = excluded.angle, post_text = excluded.post_text, at = excluded.at"
    ).bind(id, now.draft, now.angle, now.post_text, new Date().toISOString()).run();
    out.saved = fields;
  }

  if ("status" in body && body.status) {
    const r = await recordAction(env, {
      id, status: body.status, reason: body.reason, note: body.note,
    });
    return r;
  }
  return json(out);
}

// A thread read, which is dead everywhere. See the note at the top of this block.
//
// Answered with a 200 and ok:false rather than a 500, because the page treats
// this as an expected outcome and falls back to the stored body and the paste
// box. A 5xx here would read as "the board is broken" when the board is fine.
function handleFull() {
  return json({
    ok: false,
    error:
      "Reddit will not hand over a thread to anyone logged out any more, from " +
      "here or from the Mac. The post below is what the collector managed to " +
      "save. Open it in Reddit and paste it in if you need the rest.",
  });
}

// Accept a queue push over HTTPS.
//
// The alternative was `wrangler d1 execute` from the collecting Mac, which needs
// that machine to hold Cloudflare credentials. wrangler authenticates per
// machine with its own OAuth login, so a second collector meant either a second
// interactive login or a scoped API token sitting in a file. This endpoint
// removes that: the collector needs the cockpit key it already has, and nothing
// else. Any Mac that can reach this can be the collector.
//
// The queue is a projection, so replacing it wholesale is correct. `actions`,
// the only irreplaceable table up here, is never touched by this.
const QUEUE_COLS = [
  "id", "rank", "lane", "subreddit", "title", "url", "snippet", "author",
  "age_hours", "num_comments", "score", "fit", "llm_score", "why",
  "post_rule", "matched_cluster", "matched_phrase", "found_via", "source",
  "first_seen", "is_new", "stale", "archived", "locked",
];

async function handleIngest(env, body) {
  const threads = Array.isArray(body.threads) ? body.threads : null;
  if (!threads) return json({ ok: false, error: "threads array required" }, 400);
  // The board's own tables, made here on the first push that carries them. This
  // is what lets the Worker be deployed and the Mac start pushing the richer
  // payload without anyone having to remember a `wrangler d1 execute` in
  // between, which is exactly the step that would be forgotten and would show
  // up as an empty board rather than as a missing migration.
  await env.DB.batch(DDL.map((q) => env.DB.prepare(q)));
  if (threads.length > 5000) {
    return json({ ok: false, error: "too many threads in one push" }, 413);
  }

  const placeholders = QUEUE_COLS.map(() => "?").join(", ");
  const insert = `INSERT INTO queue (${QUEUE_COLS.join(", ")}) VALUES (${placeholders})`;

  const stmts = [env.DB.prepare("DELETE FROM queue")];
  threads.forEach((t, i) => {
    const vals = QUEUE_COLS.map((c) => {
      if (c === "rank") return i;
      if (c === "is_new" || c === "stale") return t[c] ? 1 : 0;
      if (c === "archived" || c === "locked") return threeState(t[c]);
      const v = t[c];
      return v === undefined ? null : v;
    });
    stmts.push(env.DB.prepare(insert).bind(...vals));
  });

  // Everything else the board needs rides along with the same push, and every
  // one of these is a projection of a table on the Mac, so replacing each
  // wholesale is correct. A push that carries none of them leaves the previous
  // ones standing, which is what lets an older collector keep working.
  const rows = (name, cols, list, cap) => {
    if (!Array.isArray(list)) return null;
    const take = list.slice(0, cap);
    stmts.push(env.DB.prepare(`DELETE FROM ${name}`));
    const marks = cols.map(() => "?").join(", ");
    const sql = `INSERT OR REPLACE INTO ${name} (${cols.join(", ")}) VALUES (${marks})`;
    take.forEach((r, i) => {
      stmts.push(env.DB.prepare(sql).bind(...cols.map((c) => {
        if (c === "rank") return i;
        // The Mac sends comments as an array and `rank_score` under the name
        // `rank`, because that is what shortlist.build calls it. Both are
        // normalised here rather than on the Mac, so the Mac keeps speaking its
        // own vocabulary and one translation lives in one place.
        if (c === "comments") {
          return Array.isArray(r.comments) ? JSON.stringify(r.comments)
                                           : (r.comments ?? null);
        }
        // The Mac sends these as they came out of its own store, which is 1, 0
        // or nothing. Normalised anyway, so the day a caller sends a real
        // boolean it lands as a number instead of failing the whole push.
        if (c === "archived" || c === "locked") return threeState(r[c]);
        const v = r[c];
        return v === undefined ? null : v;
      })));
    });
    return take.length;
  };

  const picks = rows("picks", PICK_COLS, body.picks, 200);
  const people = rows("people", PEOPLE_COLS, body.people, 500);
  const replies = rows("replies", REPLY_COLS, body.replies, 2000);
  let communities = null;
  if (Array.isArray(body.new_communities)) {
    stmts.push(env.DB.prepare("DELETE FROM communities"));
    communities = body.new_communities.slice(0, 50);
    for (const c of communities) {
      stmts.push(
        env.DB.prepare(
          "INSERT OR REPLACE INTO communities (subreddit, n, good, hit_rate, avg_score) " +
          "VALUES (?, ?, ?, ?, ?)"
        ).bind(c.subreddit ?? null, c.n ?? null, c.good ?? null,
               c.hit_rate ?? null, c.avg_score ?? null)
      );
    }
    communities = communities.length;
  }

  // The small answers the Mac computed that are not rows: the day's counts, the
  // score bands, the health of the loop, and the personas. Stored as JSON
  // strings in `meta` rather than given tables of their own, because none of
  // them is ever queried, only handed straight back to the page.
  const asJson = (v) => (v == null ? null : JSON.stringify(v));
  const personas = {};
  for (const [id, text] of Object.entries(body.personas || {})) {
    if (typeof text === "string" && text) {
      personas[`persona_${String(id).slice(0, 40)}`] = text;
    }
  }
  for (const [k, v] of Object.entries({
    generated: body.generated ?? null,
    pushed_at: new Date().toISOString(),
    pushed_by: String(body.pushed_by || "unknown").slice(0, 60),
    persona_system: body.persona_system ?? null,
    counts: asJson(body.counts),
    bar: asJson(body.bar),
    asks: asJson(body.asks),
    health: asJson(body.health),
    voice: asJson(body.voice),
    window_hours: body.window_hours != null ? String(body.window_hours) : null,
    widened: body.widened != null ? String(body.widened) : null,
    freshest_hours: body.freshest_hours != null ? String(body.freshest_hours) : null,
    survey_url: body.survey_url ?? null,
    ...personas,
  })) {
    if (v === null) continue;
    stmts.push(
      env.DB.prepare(
        "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v"
      ).bind(k, v)
    );
  }

  // One batch, so a push either lands whole or not at all. A half-written queue
  // would show the phone a truncated list that looks complete.
  await env.DB.batch(stmts);
  return json({ ok: true, threads: threads.length, picks, people, replies,
                communities });
}

async function readBody(request) {
  try {
    return await request.json();
  } catch {
    return null;
  }
}

async function recordAction(env, { id, status, reason, note }) {
  if (!id || !status) return json({ ok: false, error: "id and status required" }, 400);
  const at = new Date().toISOString();
  await env.DB.prepare(
    "INSERT INTO actions (at, post_id, status, reason, note, origin) VALUES (?, ?, ?, ?, ?, 'phone')"
  )
    .bind(at, id, status, reason || "", note || "")
    .run();
  return json({ ok: true, id, status, at });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (!env.COCKPIT_KEY) {
      return json({ ok: false, error: "COCKPIT_KEY secret is not set" }, 500);
    }

    // The install assets must be reachable WITHOUT the key. Android fetches the
    // manifest and icons out of band, often with no cookie, so gating them makes
    // the page install as a nameless generic shortcut. Neither file says anything
    // private: a title and a picture of three bars.
    const PUBLIC_ASSETS = new Set([
      "/manifest.webmanifest", "/icon-192.png", "/icon-512.png",
    ]);
    if (PUBLIC_ASSETS.has(path) && request.method === "GET") {
      const asset = await env.ASSETS.fetch(new Request(new URL(path, url), request));
      const out = new Response(asset.body, asset);
      out.headers.set("Cache-Control", "public, max-age=86400");
      out.headers.set("X-Robots-Tag", "noindex, nofollow");
      return out;
    }

    const key = presentedKey(request, url);
    if (!safeEqual(key || "", env.COCKPIT_KEY)) {
      return new Response("This board needs its key.", {
        status: 401,
        headers: { "Content-Type": "text/plain" },
      });
    }

    // Only set the cookie when the key arrived in the URL, so the bookmark works
    // once and every later request is clean.
    const setCookie =
      url.searchParams.get("k") &&
      `ck=${env.COCKPIT_KEY}; Path=/; Max-Age=31536000; HttpOnly; Secure; SameSite=Lax`;

    // Brings the shape forward before anything reads it. Returns immediately
    // after the first request of an isolate's life.
    await ensureSchema(env);

    const post = request.method === "POST";
    let res;
    if (path === "/api/reddit" && request.method === "GET") {
      res = await handleQueue(env);
    } else if (path === "/api/reddit/shortlist" && request.method === "GET") {
      res = await handleShortlist(env, url);
    } else if (path === "/api/reddit/prospects" && request.method === "GET") {
      res = await handleProspects(env);
    } else if (path === "/api/reddit/item" && post) {
      const b = await readBody(request);
      res = b ? await handleItem(env, b) : json({ ok: false, error: "bad json" }, 400);
    } else if (path === "/api/reddit/prospect" && post) {
      // Saving someone is NOT a status.
      //
      // This used to write `status: "prospect"` into `actions`, which the Mac
      // replays through `store.apply_action`, which does
      // `UPDATE posts SET status = ?`. So a save from the phone either
      // overwrote a real decision or, if the post had been decided more
      // recently, was silently dropped. Checked on 2026-10-17: eight saves had
      // been recorded that way since September, and all seven people they
      // referred to came back with `prospect = 0` on the Mac. Every one of them
      // was lost. On the Mac this is a separate column, set by
      // `store.set_prospect`, orthogonal to what you did about the thread, and
      // it travels as a decision of its own now.
      const b = await readBody(request);
      if (!b || !b.id) {
        res = json({ ok: false, error: "id required" }, 400);
      } else {
        const at = await recordDecision(env, "prospect", String(b.id),
                                        { on: !!b.on, note: b.note || "" });
        res = json({ ok: true, id: b.id, on: !!b.on, at });
      }
    } else if (path === "/api/reddit/full" && post) {
      res = handleFull();
    } else if (path === "/api/reddit/revise" && post) {
      const b = await readBody(request);
      res = b ? await handleRevise(env, b) : json({ ok: false, error: "bad json" }, 400);
    } else if (path === "/api/reddit/ask" && post) {
      const b = await readBody(request);
      res = b ? await handleAsk(env, b) : json({ ok: false, error: "bad json" }, 400);
    } else if (path === "/api/reddit/ask-sent" && post) {
      // Recorded against the PERSON, not the thread. The same human turns up in
      // three threads and being pitched three times is how a good conversation
      // becomes spam, so the unit of "already asked" is u/them.
      const b = await readBody(request);
      const who = String((b && b.author) || "").replace(/^\/?u\//, "").toLowerCase();
      if (!who) {
        res = json({ ok: false, error: "author required" }, 400);
      } else {
        const at = await recordDecision(env, "ask_sent", who, {
          post_id: (b && b.id) || null, reply_id: (b && b.reply_id) || null,
          channel: (b && b.channel) || "thread",
          text: String((b && b.text) || "").slice(0, 4000),
        });
        res = json({ ok: true, author: who, at });
      }
    } else if (path === "/api/reddit/ask-outcome" && post) {
      const b = await readBody(request);
      const who = String((b && b.author) || "").replace(/^\/?u\//, "").toLowerCase();
      const outcome = String((b && b.outcome) || "sent");
      if (!who) {
        res = json({ ok: false, error: "author required" }, 400);
      } else {
        const at = await recordDecision(env, "ask_outcome", who, { outcome });
        res = json({ ok: true, author: who, outcome, at });
      }
    } else if (path === "/api/reddit/reply-ack" && post) {
      const b = await readBody(request);
      if (!b || !b.id) {
        res = json({ ok: false, error: "id required" }, 400);
      } else {
        const on = b.on === undefined ? true : !!b.on;
        const at = await recordDecision(env, "reply_ack", String(b.id), { on });
        res = json({ ok: true, id: b.id, on, at });
      }
    } else if (path === "/api/reddit/event" && post) {
      // The trail of how a reply got written, which is the only record of what
      // the board is actually used for. Never allowed to fail the thing it was
      // measuring, so a bad body is a 200 with ok:false.
      const b = await readBody(request);
      if (b) {
        await recordDecision(env, "event", String(b.id || ""), {
          kind: String(b.kind || "note").slice(0, 40),
          detail: b.detail && typeof b.detail === "object" ? b.detail : null,
        });
      }
      res = json({ ok: !!b });
    } else if (path === "/api/reddit/transcribe" && post) {
      // No speech model is reachable from here. See the note at the top of the
      // real-board block. The page is told this in `can_transcribe` and does not
      // show a record button, so reaching this means something called it
      // anyway: answer plainly rather than with a generic 404.
      res = json({
        ok: false,
        error: "There is no transcriber here. Use the mic key on the keyboard, " +
               "which types on the device anyway.",
      }, 501);
    } else if (path === "/api/reddit/decisions" && request.method === "GET") {
      // The Mac pulls these down beside `/actions` and replays each kind into
      // the right place in its own store.
      const since = url.searchParams.get("since") || "";
      const q = await env.DB.prepare(
        "SELECT at, kind, subject, payload, origin FROM decisions " +
        "WHERE at > ? ORDER BY at ASC"
      ).bind(since).all();
      const drafts = await env.DB.prepare(
        "SELECT post_id, draft, angle, post_text, at FROM drafts " +
        "WHERE at > ? ORDER BY at ASC"
      ).bind(since).all();
      res = json({ ok: true, since, decisions: q.results || [],
                   drafts: drafts.results || [] });
    } else if (path === "/api/reddit/queue" && post) {
      const b = await readBody(request);
      res = b ? await handleIngest(env, b) : json({ ok: false, error: "bad json" }, 400);
    } else if (path === "/api/reddit/draft" && request.method === "POST") {
      const b = await readBody(request);
      res = b ? await handleDraft(env, b) : json({ ok: false, error: "bad json" }, 400);
    } else if (path === "/api/reddit/actions" && request.method === "GET") {
      // The Mac pulls these back down and replays them into the real store.
      const since = url.searchParams.get("since") || "";
      const q = await env.DB.prepare(
        "SELECT at, post_id, status, reason, note, origin FROM actions WHERE at > ? ORDER BY at ASC"
      )
        .bind(since)
        .all();
      res = json({ ok: true, since, actions: q.results || [] });
    } else if (path === "/api/reddit/enrich" && request.method === "POST") {
      // Deliberately not implemented here. Enrichment calls Anthropic, and the
      // key for that lives in the draft proxy Worker with its own credit ledger
      // and kill switch. Duplicating it here would duplicate the blast radius.
      res = json(
        {
          ok: false,
          error: "Analysis runs on the Mac board, not the phone. Credits are not wired here.",
        },
        501
      );
    } else if (path === "/" || path === "/m" || path === "/m.html") {
      res = await env.ASSETS.fetch(new Request(new URL("/m.html", url), request));
      res = new Response(res.body, res);
    } else {
      res = new Response("not found", { status: 404 });
    }

    if (setCookie) {
      res = new Response(res.body, res);
      res.headers.append("Set-Cookie", setCookie);
    }
    // This board should never be cached by an intermediary or indexed.
    res.headers.set("Cache-Control", "no-store");
    res.headers.set("X-Robots-Tag", "noindex, nofollow");
    return res;
  },
};
