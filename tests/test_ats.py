import json
from datetime import timezone
from pathlib import Path

from jobmaxxing.sources.ats import parse_ashby, parse_greenhouse, parse_lever

FIX = Path(__file__).parent / "fixtures"


def test_parse_greenhouse():
    payload = json.loads((FIX / "greenhouse.json").read_text())
    rec = parse_greenhouse(payload, company="Acme")[0]
    assert rec.source == "greenhouse"
    assert rec.company == "Acme"
    assert rec.title == "Software Engineer Intern"
    assert rec.url == "https://boards.greenhouse.io/acme/jobs/1001"
    assert rec.external_id == "1001"
    assert rec.location == "New York, NY"
    assert "software engineer intern" in rec.description.lower()
    assert rec.posted_at.tzinfo is not None
    assert rec.dedupe_key == "acme|software engineer intern"


def test_parse_lever():
    payload = json.loads((FIX / "lever.json").read_text())
    rec = parse_lever(payload, company="Acme")[0]
    assert rec.source == "lever"
    assert rec.title == "Quant Developer Intern"
    assert rec.url == "https://jobs.lever.co/acme/lev-2002"
    assert rec.external_id == "lev-2002"
    assert rec.location == "Chicago, IL"
    assert rec.description.startswith("Low-latency")
    assert rec.posted_at.tzinfo == timezone.utc


def test_parse_ashby():
    payload = json.loads((FIX / "ashby.json").read_text())
    rec = parse_ashby(payload, company="Acme")[0]
    assert rec.source == "ashby"
    assert rec.title == "ML Engineer Intern"
    assert rec.url == "https://jobs.ashbyhq.com/acme/ash-3003"
    assert rec.external_id == "ash-3003"
    assert rec.location == "Remote"
    assert rec.posted_at.tzinfo is not None


def test_ats_parsers_skip_entries_missing_required_fields():
    assert parse_greenhouse({"jobs": [{"id": 9}]}, company="Acme") == []
    assert parse_lever([{"id": "x"}], company="Acme") == []
    assert parse_ashby({"jobs": [{"id": "x"}]}, company="Acme") == []


def test_ats_parsers_skip_non_dict_entries():
    assert parse_greenhouse({"jobs": [None, "x", 5]}, company="Acme") == []
    assert parse_lever([None, "x", 5], company="Acme") == []
    assert parse_ashby({"jobs": [None, "x"]}, company="Acme") == []


def test_ats_parsers_tolerate_bad_optional_fields():
    gh = parse_greenhouse(
        {"jobs": [{"id": 1, "title": "T", "absolute_url": "https://u", "location": "Remote"}]},
        company="Acme",
    )[0]
    assert gh.location == "Remote"   # string location tolerated (not just {"name": ...})
    assert gh.posted_at is None      # missing updated_at -> None, not a crash

    lev = parse_lever(
        [{"id": "x", "text": "T", "hostedUrl": "https://u", "createdAt": "nope"}],
        company="Acme",
    )[0]
    assert lev.posted_at is None     # non-numeric createdAt -> None

    ash = parse_ashby(
        {"jobs": [{"id": "x", "title": "T", "jobUrl": "https://u", "publishedAt": "not-a-date"}]},
        company="Acme",
    )[0]
    assert ash.posted_at is None     # unparseable date -> None


def test_ats_parsers_handle_empty_or_non_collection_payload():
    assert parse_greenhouse({}, company="Acme") == []
    assert parse_greenhouse({"jobs": "oops"}, company="Acme") == []
    assert parse_lever("oops", company="Acme") == []
    assert parse_ashby({}, company="Acme") == []
