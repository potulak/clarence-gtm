-- Record a sitting even when nobody answers anything.
--
-- The endpoint used to reject a save with no answers in it, so a person who
-- pressed Start, read a few questions and closed the tab left no row at all.
-- That made the one number this survey exists to produce unreadable: a
-- drop-off table built only from people who answered something cannot tell
-- "left at question one" apart from "read all fifteen and answered none".
--
-- `reached` is the furthest question the page showed them, 1-based. Someone
-- who presses Start and leaves is now reached = 1, answered = 0.
--
-- Rows written before this ran keep reached = 0, which means unknown rather
-- than "never started". There is one such row and it answered question 8.
--
-- Apply remotely:
--   npx wrangler d1 execute clarence-waitlist --remote --file=migrations/0004_survey_reached.sql

ALTER TABLE survey ADD COLUMN reached INTEGER NOT NULL DEFAULT 0;
