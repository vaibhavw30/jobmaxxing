create table if not exists jobs (
  id              uuid primary key default gen_random_uuid(),

  dedupe_key      text not null,
  source          text not null,
  external_id     text,

  company         text not null,
  title           text not null,
  location        text,
  url             text not null,
  alt_urls        text[] not null default '{}',
  description     text,
  posted_at       timestamptz,
  is_active       boolean not null default true,

  scraped_at      timestamptz not null default now(),

  -- later-phase columns (unused this sprint)
  resume_type     text,
  route_method    text,
  route_confidence real,
  status          text not null default 'new',
  artifact_prefix text,
  score_before    jsonb,
  score_after     jsonb,
  notes           text,

  unique (dedupe_key)
);

create index if not exists jobs_is_active_idx on jobs (is_active);
create index if not exists jobs_source_idx on jobs (source);
create index if not exists jobs_scraped_at_idx on jobs (scraped_at desc);
create index if not exists jobs_status_idx on jobs (status);
