from datetime import date, datetime, timezone

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.discovery.jobspy_source import discover_jobspy


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def test_discover_ingests_indeed_and_is_failsoft_on_linkedin(conn):
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def fake_scrape(search):
        if search["site"] == "linkedin":
            raise RuntimeError("429 blocked")
        return [{"title": "SWE Intern", "company": "Acme",
                 "job_url": "https://www.indeed.com/viewjob?jk=abc123",
                 "description": "api work", "date_posted": date(2026, 6, 20), "location": "Remote"}]

    config = {"sites": ["indeed", "linkedin"], "search_terms": ["software engineer intern"],
              "results_wanted": {"indeed": 10, "linkedin": 10}, "location": "United States"}
    report = discover_jobspy(conn, scrape=fake_scrape, config=config, now=now)

    assert report["jobspy:indeed:software-engineer-intern"]["status"] == "ok"
    assert report["jobspy:linkedin:software-engineer-intern"]["status"] == "error"

    row = conn.execute(
        "select company, title, source, description, url from jobs"
    ).fetchone()
    assert row[:4] == ("Acme", "SWE Intern", "jobspy:indeed", "api work")
    assert row[4] == "https://www.indeed.com/viewjob?jk=abc123"   # Task-1 fix: jk preserved end-to-end


def test_discover_dedupes_same_posting_across_terms(conn):
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def fake_scrape(search):
        return [{"title": "SWE Intern", "company": "Acme",
                 "job_url": "https://www.indeed.com/viewjob?jk=abc123",
                 "date_posted": date(2026, 6, 20)}]

    config = {"sites": ["indeed"], "search_terms": ["term a", "term b"],
              "results_wanted": {"indeed": 10}}
    discover_jobspy(conn, scrape=fake_scrape, config=config, now=now)
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 1   # one dedupe_key
