-- URL verification: whether a posting's link still resolves, with the same attempt-cap shape as
-- enrich/recover. url_status: NULL=unverified, 'alive', 'dead'. A 'dead' row has verify_attempts
-- set to the cap so it isn't reselected until the operator bumps the cap. No backfill: every row
-- starts unverified.
alter table jobs add column if not exists url_status      text;
alter table jobs add column if not exists verify_attempts int not null default 0;
alter table jobs add column if not exists verified_at      timestamptz;
alter table jobs add column if not exists verify_error     text;
