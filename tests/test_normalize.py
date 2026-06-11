from datetime import datetime, timedelta, timezone

from jobmaxxing.normalize import (
    ATS_SOURCES,
    canonicalize_url,
    make_dedupe_key,
    within_age_cutoff,
)


def test_dedupe_key_collapses_case_punctuation_whitespace():
    a = make_dedupe_key("Acme, Inc.", "Software   Engineer Intern")
    b = make_dedupe_key("acme inc", "software engineer intern")
    assert a == b
    assert a == "acme inc|software engineer intern"


def test_dedupe_key_distinguishes_different_titles():
    assert make_dedupe_key("Acme", "SWE Intern") != make_dedupe_key("Acme", "ML Intern")


def test_canonicalize_url_strips_query_fragment_and_trailing_slash():
    url = "HTTPS://Boards.Greenhouse.io/acme/jobs/123/?utm_source=x#apply"
    assert canonicalize_url(url) == "https://boards.greenhouse.io/acme/jobs/123"


def test_canonicalize_url_keeps_root_path():
    assert canonicalize_url("https://acme.com/") == "https://acme.com/"


def test_age_cutoff_keeps_recent_and_null_dates():
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    assert within_age_cutoff(now - timedelta(days=10), now) is True
    assert within_age_cutoff(None, now) is True  # no date -> never drop


def test_age_cutoff_rejects_old():
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    assert within_age_cutoff(now - timedelta(days=300), now) is False


def test_ats_sources_constant():
    assert ATS_SOURCES == {"greenhouse", "lever", "ashby"}
