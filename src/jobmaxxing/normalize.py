import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

ATS_SOURCES = {"greenhouse", "lever", "ashby"}

# ~8 months
MAX_AGE_DAYS = 243

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
    """Lowercase scheme+host, drop query/fragment, strip trailing slash (keep root)."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def within_age_cutoff(
    posted_at: datetime | None, now: datetime, max_age_days: int = MAX_AGE_DAYS
) -> bool:
    """True if the posting should be ingested. Null dates are kept (we lack evidence it's stale)."""
    if posted_at is None:
        return True
    return posted_at >= now - timedelta(days=max_age_days)
