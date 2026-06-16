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

# Term filtering: a posting's recruiting term(s) are "in window" if their year is in the
# current cycle. The window is derived from the run date (no hand-maintained list): the current
# calendar year, plus next year once we're in H2 — by late summer next year's cycle is open for
# applications, so we surface it without dropping the current fall.
CYCLE_LOOKAHEAD_MONTH = 7  # July

_TERM_RE = re.compile(r"\b(spring|summer|fall|winter)\s+(\d{4})\b")


def current_cycle_years(today: date) -> set[int]:
    """In-window years for term filtering, derived from ``today``."""
    years = {today.year}
    if today.month >= CYCLE_LOOKAHEAD_MONTH:
        years.add(today.year + 1)
    return years


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
