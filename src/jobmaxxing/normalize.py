import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

ATS_SOURCES = {"greenhouse", "lever", "ashby"}

# ~8 months
MAX_AGE_DAYS = 243

# NOTE: non-ASCII/accented characters are stripped by this regex — e.g.
# "L'Oréal" → "l oral" — which can cause dedupe false-negatives for companies
# with accented names. This is an accepted open item per spec §11
# (title/company normalization aggressiveness).
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")

# Term filtering: a posting's recruiting term is "in window" if it is UPCOMING — its season has
# not started yet — within the next TERM_HORIZON_MONTHS. Derived from the run date so it auto-rolls
# and never needs manual bumping. As of June 2026 this is {Fall 2026, Winter 2026, Spring 2027,
# Summer 2027}: Summer 2026 (started ~May) and earlier terms drop out; Fall 2027+ is too far ahead.
SEASON_START_MONTH = {"spring": 1, "summer": 5, "fall": 9, "winter": 12}
TERM_HORIZON_MONTHS = 12

_TERM_RE = re.compile(r"\b(spring|summer|fall|winter)\s+(\d{4})\b")


def _term_start_index(season: str, year: int) -> int:
    """Months-since-year-0 ordinal of a term's (approximate) start, for window comparisons."""
    return year * 12 + SEASON_START_MONTH[season]


def upcoming_terms(
    today: date, horizon_months: int = TERM_HORIZON_MONTHS
) -> set[tuple[str, int]]:
    """The (season, year) terms whose start is after ``today`` and within ``horizon_months``.

    Forward-looking: a term whose season has already started (or is past) is excluded, so e.g. in
    June 2026 'Summer 2026' (starts ~May) and 'Spring 2026' (past) drop out while 'Fall 2026' …
    'Summer 2027' stay in. Auto-rolls with the date — no hand-maintained list."""
    now = today.year * 12 + today.month
    return {
        (season, year)
        for year in range(today.year, today.year + 3)
        for season in SEASON_START_MONTH
        if now < _term_start_index(season, year) <= now + horizon_months
    }


def term_label(season: str, year: int) -> str:
    """Canonical display/storage form of a term, e.g. ('summer', 2026) -> 'Summer 2026'."""
    return f"{season.capitalize()} {year}"


def in_window_term_labels(today: date) -> set[str]:
    """Canonical labels of the upcoming terms as of ``today`` (for triage's date-aware demotion)."""
    return {term_label(season, year) for season, year in upcoming_terms(today)}


def parse_term(value) -> tuple[str, int] | None:
    """Parse a Simplify ``terms`` entry like 'Summer 2026' -> ('summer', 2026).

    Returns None for 'N/A', blanks, non-strings, or anything without a season+year. Matching runs
    on ``normalize_text`` output, so case and surrounding/extra whitespace don't matter."""
    if not isinstance(value, str):
        return None
    m = _TERM_RE.search(normalize_text(value))
    if not m:
        return None
    return m.group(1), int(m.group(2))


def normalize_text(value: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    value = value.lower().strip()
    value = _PUNCT.sub(" ", value)
    value = _WS.sub(" ", value).strip()
    return value


def make_dedupe_key(company: str, title: str) -> str:
    """Soft cross-source collapse key: normalized(company) | normalized(title)."""
    return f"{normalize_text(company)}|{normalize_text(title)}"


def canonicalize_url(url: str) -> str:
    """Lowercase scheme+host, drop query/fragment, strip trailing slash (keep root).

    CAVEAT: stripping ALL query params is safe for tracking-style params
    (e.g. ``utm_*``) used by the current sources (Simplify / Greenhouse /
    Lever / Ashby), but must NOT be used for ATS boards that encode job
    identity in the query string (e.g. ``?jobId=...``).

    Non-absolute URLs (missing scheme or netloc) are returned unchanged to
    avoid corrupting relative or schemeless inputs.
    """
    stripped = url.strip()
    parts = urlsplit(stripped)
    if not parts.scheme or not parts.netloc:
        return stripped
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def within_age_cutoff(
    posted_at: datetime | None, now: datetime, max_age_days: int = MAX_AGE_DAYS
) -> bool:
    """True if the posting should be ingested. Null dates are kept (we lack evidence it's stale).

    Both ``posted_at`` and ``now`` are coerced to UTC when timezone-naive, so a missing
    tzinfo on either side never raises a TypeError (naive vs. aware comparison) in the pipeline.
    """
    if posted_at is None:
        return True
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return posted_at >= now - timedelta(days=max_age_days)
