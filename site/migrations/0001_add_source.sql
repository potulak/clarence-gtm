-- Migration 0001: add the first-touch source bucket to an existing waitlist DB.
-- schema.sql already includes this column for fresh installs; this is only for
-- the already-created remote D1 (which CREATE TABLE IF NOT EXISTS won't alter).
--
-- Apply remotely:
--   npx wrangler d1 execute clarence-waitlist --remote --file=migrations/0001_add_source.sql
-- Apply locally:
--   npx wrangler d1 execute clarence-waitlist --local  --file=migrations/0001_add_source.sql
--
-- SQLite has no ADD COLUMN IF NOT EXISTS. If the column already exists this
-- errors harmlessly ("duplicate column name: source"); ignore it.

ALTER TABLE waitlist ADD COLUMN source TEXT;
CREATE INDEX IF NOT EXISTS idx_waitlist_source ON waitlist (source);
