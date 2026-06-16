from datetime import date, datetime, timedelta, timezone

from jobmaxxing.normalize import (
    ATS_SOURCES,
    canonicalize_url,
    in_window_term_labels,
    make_dedupe_key,
    normalize_text,
    parse_term,
    term_label,
    upcoming_terms,
    within_age_cutoff,
)


# ---------------------------------------------------------------------------
# normalize_text (Fix 4)
# ---------------------------------------------------------------------------


def test_normalize_text():
    assert normalize_text("Acme, Inc.") == "acme inc"
    assert normalize_text("??!") == ""
    assert normalize_text("a\t b\nc") == "a b c"


# ---------------------------------------------------------------------------
# make_dedupe_key
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# within_age_cutoff – naive datetime coercion (Fix 1)
# ---------------------------------------------------------------------------


def test_age_cutoff_coerces_naive_datetime_as_utc():
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    # Naive recent datetime — must NOT raise, must return True
    naive_recent = datetime(2026, 6, 1)  # 10 days ago, no tzinfo
    assert within_age_cutoff(naive_recent, now) is True
    # Naive old datetime — must return False
    naive_old = datetime(2025, 9, 1)  # > 243 days ago
    assert within_age_cutoff(naive_old, now) is False


# ---------------------------------------------------------------------------
# canonicalize_url – non-absolute URLs (Fix 2)
# ---------------------------------------------------------------------------


def test_canonicalize_url_leaves_non_absolute_unchanged():
    schemeless = "boards.greenhouse.io/acme/jobs/123"
    assert canonicalize_url(schemeless) == schemeless


def test_age_cutoff_coerces_naive_now_as_utc():
    # naive `now` must not raise against a tz-aware posted_at (symmetric coercion)
    naive_now = datetime(2026, 6, 11)
    aware_recent = datetime(2026, 6, 1, tzinfo=timezone.utc)
    aware_old = datetime(2025, 9, 1, tzinfo=timezone.utc)
    assert within_age_cutoff(aware_recent, naive_now) is True
    assert within_age_cutoff(aware_old, naive_now) is False


# ---------------------------------------------------------------------------
# upcoming_terms / term_label / parse_term
# ---------------------------------------------------------------------------


def test_upcoming_terms_keeps_next_12_months_of_unstarted_terms():
    # June 2026: Summer 2026 (started ~May) and Spring 2026 (past) drop;
    # Fall 2026 .. Summer 2027 stay.
    assert upcoming_terms(date(2026, 6, 16)) == {
        ("fall", 2026), ("winter", 2026), ("spring", 2027), ("summer", 2027),
    }


def test_upcoming_terms_excludes_started_and_far_future():
    win = upcoming_terms(date(2026, 6, 16))
    assert ("summer", 2026) not in win   # already started (~May)
    assert ("spring", 2026) not in win   # past
    assert ("fall", 2027) not in win     # beyond the 12-month horizon


def test_upcoming_terms_rolls_forward_at_year_boundary():
    # December 2026: the whole 2027 cycle is upcoming.
    assert upcoming_terms(date(2026, 12, 1)) == {
        ("spring", 2027), ("summer", 2027), ("fall", 2027), ("winter", 2027),
    }


def test_term_label_canonical():
    assert term_label("summer", 2026) == "Summer 2026"
    assert term_label("fall", 2027) == "Fall 2027"


def test_in_window_term_labels_matches_upcoming():
    assert in_window_term_labels(date(2026, 6, 16)) == {
        "Fall 2026", "Winter 2026", "Spring 2027", "Summer 2027",
    }


def test_parse_term_basic():
    assert parse_term("Summer 2026") == ("summer", 2026)
    assert parse_term("Fall 2026") == ("fall", 2026)


def test_parse_term_whitespace_and_case_insensitive():
    assert parse_term("  SUMMER   2026 ") == ("summer", 2026)


def test_parse_term_returns_none_for_untagged_or_junk():
    assert parse_term("N/A") is None
    assert parse_term("") is None
    assert parse_term("intern") is None
    assert parse_term(None) is None
    assert parse_term(["Summer 2026"]) is None
