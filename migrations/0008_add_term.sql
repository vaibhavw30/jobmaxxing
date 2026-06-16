-- Per-posting recruiting term(s), e.g. {'Summer 2026','Fall 2026'}, parsed from the Simplify
-- feed's `terms` field. Nullable text[]: NULL = legacy/unprocessed or ATS (no term concept);
-- '{}' = processed but untagged (kept N/A / co-op-with-no-term); non-empty = matched in-window
-- terms. Used to demote off-window/legacy github rows in triage and to filter by term.
-- No backfill: existing rows stay NULL and self-heal on the next term-aware ingest.
alter table jobs add column if not exists term text[];
