from jobmaxxing.merge import merge_records
from jobmaxxing.models import JobRecord


def _rec(**kw):
    base = dict(source="github:simplify", company="Acme", title="SWE Intern", url="https://list/apply", dedupe_key="acme|swe intern")
    base.update(kw)
    return JobRecord(**base)


def test_merge_fills_description_when_existing_null():
    existing = _rec(description=None)
    incoming = _rec(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1", description="full JD")
    merged = merge_records(existing, incoming)
    assert merged.description == "full JD"


def test_merge_keeps_existing_description_when_present():
    existing = _rec(description="original")
    incoming = _rec(description="newer")
    merged = merge_records(existing, incoming)
    assert merged.description == "original"


def test_merge_promotes_ats_url_over_list_url():
    existing = _rec(source="github:simplify", url="https://list/apply")
    incoming = _rec(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1")
    merged = merge_records(existing, incoming)
    assert merged.url == "https://boards.greenhouse.io/acme/jobs/1"
    assert "https://list/apply" in merged.alt_urls
    assert merged.url not in merged.alt_urls


def test_merge_does_not_demote_existing_ats_url_for_list_url():
    existing = _rec(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1")
    incoming = _rec(source="github:simplify", url="https://list/apply")
    merged = merge_records(existing, incoming)
    assert merged.url == "https://boards.greenhouse.io/acme/jobs/1"
    assert "https://list/apply" in merged.alt_urls


def test_merge_never_loses_a_url_and_dedups_alt_urls():
    existing = _rec(url="https://a", alt_urls=["https://b"])
    incoming = _rec(url="https://a", alt_urls=["https://b", "https://c"])
    merged = merge_records(existing, incoming)
    assert set(merged.alt_urls) == {"https://b", "https://c"}
    assert merged.url == "https://a"


def test_merge_fills_external_id_and_location_when_missing():
    existing = _rec(external_id=None, location=None)
    incoming = _rec(source="greenhouse", external_id="gh-1", location="NYC")
    merged = merge_records(existing, incoming)
    assert merged.external_id == "gh-1"
    assert merged.location == "NYC"


def test_merge_refreshes_is_active_from_incoming():
    existing = _rec(is_active=True)
    incoming = _rec(is_active=False)
    merged = merge_records(existing, incoming)
    assert merged.is_active is False
