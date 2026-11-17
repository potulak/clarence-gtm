-- Question 1 stopped being a bare text box.
--
-- It asked "is there someone you have lost touch with?" and gave a textarea,
-- so the quick honest answer, yes, had nowhere to go. The first real visitor
-- to reach the survey tapped past it, then past the six questions that only
-- make sense once it is answered, and answered question 8, the first one that
-- restates its own subject. The question is now four options plus an optional
-- "who, roughly?" box, so `who` holds the choice and `who_what` holds the
-- prose that used to be the only thing on offer.
--
-- Rows written before this ran have the old free text sitting in `who`.
-- Nothing is migrated: `who_what` starts null for them, and a value in `who`
-- that is not one of the four option slugs is an answer from the old shape.
--
-- Apply remotely:
--   npx wrangler d1 execute clarence-waitlist --remote --file=migrations/0003_survey_who_what.sql

ALTER TABLE survey ADD COLUMN who_what TEXT;
