-- Count of jobs in each funnel stage.
create or replace view funnel_counts as
  select status, count(*) as n
  from jobs
  group by status
  order by status;

-- Tailored jobs awaiting the operator's review/decision, with their scores.
create or replace view review_queue as
  select id, company, title, resume_type, score_before, score_after, artifact_prefix, scraped_at
  from jobs
  where status = 'tailored'
  order by scraped_at desc;
