-- Enrichment tracking: how many fetch attempts a row has had, when it was last
-- enriched, and the last error. Permanent failures are marked by setting
-- enrich_attempts to the cap so the candidate query stops reselecting them.
alter table jobs add column if not exists enrich_attempts int not null default 0;
alter table jobs add column if not exists enriched_at     timestamptz;
alter table jobs add column if not exists enrich_error    text;
