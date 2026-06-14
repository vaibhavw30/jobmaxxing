-- JD recovery (find-elsewhere): how a description was obtained, and recovery retry tracking.
alter table jobs add column if not exists jd_source        text;    -- 'recovered' (set by find-elsewhere); NULL = from the ATS/enrichment path
alter table jobs add column if not exists recover_attempts int not null default 0;
alter table jobs add column if not exists recover_error    text;
