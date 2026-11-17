-- Migration 0002: the survey table.
--
-- The waitlist answers "tell me when it ships". This answers something more
-- useful while there is nothing to ship: what the problem actually is, in
-- their words, from someone who was talking about it on Reddit ten seconds ago.
--
-- Two things shape this table.
--
-- Every answer is optional and rows are written as people go, not on submit.
-- Someone who answers three questions and closes the tab has still told us
-- something, and the old shape threw that away. `response_id` is generated in
-- the browser and each save upserts on it, so one person is one row however
-- many times they come back to it.
--
-- `completed` marks the ones who reached the end. Partial rows are the honest
-- majority and should be read as such: a 40% drop-off at question four is a
-- finding about question four.
--
-- Apply remotely:
--   npx wrangler d1 execute clarence-waitlist --remote --file=migrations/0002_survey.sql
-- Apply locally:
--   npx wrangler d1 execute clarence-waitlist --local  --file=migrations/0002_survey.sql

CREATE TABLE IF NOT EXISTS survey (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  response_id TEXT NOT NULL UNIQUE,

  -- The ten, in the order they are asked.
  who TEXT,           -- 1. who came to mind
  how_long TEXT,      -- 2. how long has it been
  how_faded TEXT,     -- 3. how it happened
  stops TEXT,         -- 4. what stops them sending it
  what_say TEXT,      -- 5. how writing it would go: know / stare / bland / unsent
  what_say_what TEXT, -- 5b. the message itself, from those who know it
  how_many TEXT,      -- 6. how many people this is true of
  tried TEXT,         -- 7. ever tried to fix it: never / gave-up / still
  tried_what TEXT,    -- 7b. what it was and what happened to it
  last_reached TEXT,  -- 8. when they last actually reached out
  how_goes TEXT,      -- 9. how it goes, several allowed
  mac_messages TEXT,  -- 10. do they text from a Mac at all
  access TEXT,        -- 11. how the reading-your-messages ask sits
  drafting TEXT,      -- 12. how they feel about it writing the message
  blockers TEXT,      -- 13. what would stop them, several allowed
  would_use TEXT,     -- 14. what they would actually do if it existed
  anything TEXT,      -- 15. anything else
  talk TEXT,          -- what they will let him come back with
  email TEXT,         -- optional, only for a reply

  answered INTEGER NOT NULL DEFAULT 0,   -- how many they filled in
  completed INTEGER NOT NULL DEFAULT 0,  -- did they reach the end

  -- Same first-touch attribution as the waitlist, so an answer is traceable
  -- back to the Reddit thread that sent it.
  source TEXT,
  page TEXT,
  utm_source TEXT,
  utm_medium TEXT,
  utm_campaign TEXT,
  utm_term TEXT,
  utm_content TEXT,
  referrer TEXT,
  landing TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_survey_created ON survey (created_at);
CREATE INDEX IF NOT EXISTS idx_survey_source ON survey (source);
CREATE INDEX IF NOT EXISTS idx_survey_completed ON survey (completed);
