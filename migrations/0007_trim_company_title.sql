-- One-off data hygiene: trim surrounding whitespace from existing company/title.
-- Scraped feeds have produced leading-space values (e.g. ' MAG Aerospace',
-- ' CCC Intelligent Solutions') that sort before alphabetical entries and display
-- with an ugly leading space in the triage UI. New ingests are trimmed at the
-- source (JobRecord.__post_init__); this backfills rows written before that fix.
--
-- Safe to leave in the always-run migration set: the WHERE clause matches only
-- dirty rows, so after the first pass it updates zero rows (true no-op). It does
-- NOT touch dedupe_key, which is already whitespace-insensitive (normalize_text
-- strips before keying), so trimming the display fields cannot collide existing keys.
update jobs
   set company = trim(company),
       title   = trim(title)
 where company <> trim(company)
    or title   <> trim(title);
