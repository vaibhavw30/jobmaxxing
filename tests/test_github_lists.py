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
