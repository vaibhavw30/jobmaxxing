from datetime import date, datetime, timezone

from jobmaxxing.discovery.jobspy_source import parse_jobspy


def test_parse_maps_all_fields():
    rows = [{
        "title": "Software Engineer Intern", "company": "Acme",
        "job_url": "https://www.indeed.com/viewjob?jk=abc123",
        "description": "Build APIs.", "location": "Remote",
        "date_posted": date(2026, 6, 20),
    }]
    rec = parse_jobspy(rows, site="indeed")[0]
    assert rec.source == "jobspy:indeed"
    assert rec.company == "Acme" and rec.title == "Software Engineer Intern"
    assert rec.url == "https://www.indeed.com/viewjob?jk=abc123"
    assert rec.external_id == "https://www.indeed.com/viewjob?jk=abc123"
    assert rec.description == "Build APIs."
    assert rec.location == "Remote"
    assert rec.posted_at == datetime(2026, 6, 20, tzinfo=timezone.utc)
    assert rec.dedupe_key == "acme|software engineer intern"

def test_parse_skips_rows_missing_required_fields():
    rows = [
        {"title": "SWE Intern", "company": "Acme"},                     # no job_url
        {"title": "SWE Intern", "job_url": "https://x/1"},              # no company
        {"company": "Acme", "job_url": "https://x/2"},                  # no title
        {"title": " ", "company": "Acme", "job_url": "https://x/3"},    # blank title
    ]
    assert parse_jobspy(rows, site="indeed") == []

def test_parse_nan_description_and_date_become_none():
    nan = float("nan")
    rows = [{"title": "SWE Intern", "company": "Acme", "job_url": "https://x/1",
             "description": nan, "date_posted": nan}]
    rec = parse_jobspy(rows, site="linkedin")[0]
    assert rec.description is None and rec.posted_at is None
    assert rec.source == "jobspy:linkedin"

def test_parse_location_from_city_state_country():
    rows = [{"title": "SWE Intern", "company": "Acme", "job_url": "https://x/1",
             "city": "Atlanta", "state": "GA", "country": "USA"}]
    assert parse_jobspy(rows, site="indeed")[0].location == "Atlanta, GA, USA"

def test_parse_iso_string_date():
    rows = [{"title": "SWE Intern", "company": "Acme", "job_url": "https://x/1",
             "date_posted": "2026-06-01"}]
    assert parse_jobspy(rows, site="indeed")[0].posted_at == datetime(2026, 6, 1, tzinfo=timezone.utc)
