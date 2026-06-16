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


def test_parse_simplify_skips_whitespace_only_company_or_title():
    # Whitespace-only fields must be rejected, not stored as empty strings after
    # JobRecord trims them (mirrors the ATS adapter's strip-then-reject behaviour).
    payload = [
        {"company_name": "   ", "title": "SWE", "url": "https://x"},
        {"company_name": "Acme", "title": "\t\n", "url": "https://x"},
    ]
    assert parse_simplify_format(payload, source="github:simplify") == []


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


def _entry(**kw):
    base = {"company_name": "Acme", "title": "SWE", "url": "https://x"}
    base.update(kw)
    return base


def test_keeps_and_tags_in_window_term():
    rec = parse_simplify_format([_entry(terms=["Summer 2026"])],
                                source="github:simplify", allowed_years={2026})[0]
    assert rec.term == ["Summer 2026"]


def test_keeps_multi_term_excludes_off_window_member():
    rec = parse_simplify_format([_entry(terms=["Fall 2026", "Summer 2027", "Spring 2026"])],
                                source="github:simplify", allowed_years={2026})[0]
    assert rec.term == ["Fall 2026", "Spring 2026"]  # 2027 dropped from the stored list


def test_drops_purely_off_window():
    assert parse_simplify_format([_entry(terms=["Summer 2027"])],
                                 source="github:simplify", allowed_years={2026}) == []
    assert parse_simplify_format([_entry(terms=["Winter 2025"])],
                                 source="github:simplify", allowed_years={2026}) == []


def test_keeps_untagged_with_empty_term():
    for terms in (None, [], ["N/A"], ["totally bogus"]):
        recs = parse_simplify_format([_entry(terms=terms)],
                                     source="github:simplify", allowed_years={2026})
        assert len(recs) == 1 and recs[0].term == []


def test_drops_mixed_real_offwindow_plus_na():
    assert parse_simplify_format([_entry(terms=["Summer 2027", "N/A"])],
                                 source="github:simplify", allowed_years={2026}) == []


def test_term_match_is_case_and_whitespace_insensitive():
    rec = parse_simplify_format([_entry(terms=[" summer  2026 "])],
                                source="github:simplify", allowed_years={2026})[0]
    assert rec.term == ["summer  2026"]  # original (stripped) string stored; matched on normalize


def test_allowed_years_none_keeps_all_and_tags():
    recs = parse_simplify_format([_entry(terms=["Summer 2027"])], source="github:simplify")
    assert len(recs) == 1 and recs[0].term == ["Summer 2027"]
