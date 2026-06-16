import json
from datetime import timezone
from pathlib import Path

from jobmaxxing.sources.github_lists import parse_simplify_format

FIXTURE = Path(__file__).parent / "fixtures" / "simplify.json"


def test_parse_simplify_maps_fields():
    payload = json.loads(FIXTURE.read_text())
    records = parse_simplify_format(payload, source="github:simplify")
    first = records[0]
    assert first.source == "github:simplify"
    assert first.company == "Acme"
    assert first.title == "Software Engineer Intern"
    assert first.location == "New York, NY, Remote"
    assert first.external_id == "abc123"
    assert first.is_active is True
    assert first.posted_at.tzinfo == timezone.utc
    assert first.dedupe_key == "acme|software engineer intern"


def test_parse_simplify_handles_empty_locations_and_inactive():
    payload = json.loads(FIXTURE.read_text())
    second = parse_simplify_format(payload, source="github:simplify")[1]
    assert second.location is None
    assert second.is_active is False


def test_parse_simplify_skips_entries_missing_required_fields():
    payload = [{"title": "No Company"}, {"company_name": "No Title"}]
    assert parse_simplify_format(payload, source="github:simplify") == []


def test_parse_simplify_skips_non_dict_entries():
    payload = [None, "garbage", 42, {"company_name": "Acme", "title": "SWE", "url": "https://x"}]
    records = parse_simplify_format(payload, source="github:simplify")
    assert len(records) == 1
    assert records[0].company == "Acme"


def test_parse_simplify_tolerates_dirty_locations():
    payload = [{"company_name": "Acme", "title": "SWE", "url": "https://x",
                "locations": ["NYC", None, 42]}]
    rec = parse_simplify_format(payload, source="github:simplify")[0]
    assert rec.location == "NYC, 42"  # None filtered, non-strings coerced


def test_parse_simplify_handles_non_numeric_date_posted():
    payload = [{"company_name": "Acme", "title": "SWE", "url": "https://x",
                "date_posted": "2024-01-15"}]
    rec = parse_simplify_format(payload, source="github:simplify")[0]
    assert rec.posted_at is None  # non-numeric date ignored, not a crash


def test_parse_simplify_treats_null_active_as_active():
    payload = [{"company_name": "Acme", "title": "SWE", "url": "https://x", "active": None}]
    rec = parse_simplify_format(payload, source="github:simplify")[0]
    assert rec.is_active is True


def test_parse_simplify_stores_company_and_title_trimmed():
    payload = [{"company_name": " CCC Intelligent Solutions", "title": "SWE Intern ",
                "url": "https://x"}]
    rec = parse_simplify_format(payload, source="github:simplify")[0]
    assert rec.company == "CCC Intelligent Solutions"
    assert rec.title == "SWE Intern"


def test_parse_simplify_threads_source_label_for_each_fork():
    payload = [{"company_name": "Acme", "title": "SWE", "url": "https://x"}]
    for label in ("github:simplify", "github:vanshb03", "github:pitt-csc"):
        assert parse_simplify_format(payload, source=label)[0].source == label
