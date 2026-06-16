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
--
-- regexp_replace (not plain trim()) is used so the backfill matches the ingest
-- path's Python str.strip(): SQL trim() removes only the ASCII space, while \s
-- here covers space, tab, newline, CR, FF and VT — the same whitespace a stray
-- tab/newline-wrapped scrape would carry.
update jobs
   set company = regexp_replace(company, '^\s+|\s+$', '', 'g'),
       title   = regexp_replace(title,   '^\s+|\s+$', '', 'g')
 where company <> regexp_replace(company, '^\s+|\s+$', '', 'g')
    or title   <> regexp_replace(title,   '^\s+|\s+$', '', 'g');
