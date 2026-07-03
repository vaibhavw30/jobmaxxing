"""JobSpy discovery source — a local, operator-run worker (residential IP; `discovery` extra).

parse_jobspy is a pure, defensive adapter (no pandas/network). Only _jobspy_scrape imports jobspy,
lazily, so this module imports fine without the extra and CI never touches JobSpy.
"""

import logging
from datetime import date, datetime, timezone

from ..models import JobRecord
from ..normalize import make_dedupe_key

logger = logging.getLogger(__name__)


def _clean_str(value):
    """A trimmed non-empty string, or None. Non-strings (incl. pandas NaN floats) -> None."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _coerce_dt(value):
    """Coerce JobSpy's date_posted to a tz-aware UTC datetime, or None (NaN/blank/unparseable)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _location(row):
    loc = _clean_str(row.get("location"))
    if loc:
        return loc
    parts = [_clean_str(row.get(k)) for k in ("city", "state", "country")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def parse_jobspy(rows, *, site):
    """Normalize JobSpy rows (DataFrame.to_dict('records')) into JobRecords. Defensive: rows missing
    title/company/job_url are skipped (fail-soft). source = f'jobspy:{site}'."""
    source = f"jobspy:{site}"
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        company = _clean_str(row.get("company"))
        title = _clean_str(row.get("title"))
        url = _clean_str(row.get("job_url"))
        if not company or not title or not url:
            continue
        records.append(JobRecord(
            source=source,
            company=company,
            title=title,
            url=url,
            external_id=url,
            location=_location(row),
            description=_clean_str(row.get("description")),
            posted_at=_coerce_dt(row.get("date_posted")),
            dedupe_key=make_dedupe_key(company, title),
        ))
    return records
