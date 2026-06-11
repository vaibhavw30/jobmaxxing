import re
from datetime import datetime, timedelta, timezone
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

    Timezone-naive ``posted_at`` values are treated as UTC before comparison
    so that a missing tzinfo never causes a TypeError in the pipeline.
    """
    if posted_at is None:
        return True
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return posted_at >= now - timedelta(days=max_age_days)
