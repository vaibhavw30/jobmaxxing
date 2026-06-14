-- The operator's nightly manual-capture worklist: relevant, still-JD-less jobs that BOTH the
-- headless worker and find-elsewhere exhausted. recover_attempts >= 2 mirrors recover_jd's
-- default cap (keep in sync). Idempotent (create or replace).
create or replace view nightly_queue as
  select id, company, title, url, resume_type, route_confidence, scraped_at
  from jobs
  where coalesce(description, '') = ''
    and resume_type is not null
    and route_method is distinct from 'manual'
    and recover_attempts >= 2
  order by scraped_at desc;
