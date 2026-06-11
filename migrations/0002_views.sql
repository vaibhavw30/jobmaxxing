-- Active postings not yet routed: the operator's working queue.
create or replace view active_unrouted as
  select id, company, title, location, url, source, posted_at, scraped_at
  from jobs
  where is_active = true and resume_type is null
  order by scraped_at desc;
