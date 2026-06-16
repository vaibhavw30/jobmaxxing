-- When a posting was FIRST ingested. scraped_at is bumped to now() on every re-poll (the merge
-- UPDATE), so it means "last seen" and can't drive a "new since yesterday" digest. first_seen_at is
-- set once (column default now() on insert) and never updated, so it answers "what's genuinely new".
--
-- Existing rows are backfilled from coalesce(posted_at, scraped_at) so the first digest after deploy
-- isn't flooded with the whole backlog as "new" (a posting posted/seen weeks ago gets an old
-- first_seen_at). Idempotent: add-if-not-exists, and the backfill only touches still-null rows, so the
-- always-re-run migration set is a no-op afterward.
alter table jobs add column if not exists first_seen_at timestamptz;
update jobs set first_seen_at = coalesce(posted_at, scraped_at) where first_seen_at is null;
alter table jobs alter column first_seen_at set default now();
