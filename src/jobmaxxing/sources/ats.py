import math
from datetime import datetime, timezone

from ..models import JobRecord
from ..normalize import make_dedupe_key


def _clean_str(value) -> str | None:
    """A trimmed non-empty string, or None. Non-strings (list/dict/int/None) become None,
    so a malformed field can never violate JobRecord's str|None contract or crash downstream."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _record(company, source, title, url, external_id, location, description, posted_at):
    title = _clean_str(title)
    url = _clean_str(url)
    if not title or not url:
        return None
    return JobRecord(
        source=source,
        company=company,
        title=title,
        url=url,
        external_id=str(external_id) if external_id is not None else None,
        location=_clean_str(location),
        description=description,
        posted_at=posted_at,
        is_active=True,
        dedupe_key=make_dedupe_key(company, title),
    )


def _parse_iso(value) -> datetime | None:
    """Parse an ISO-8601 timestamp; None for missing/non-string/unparseable values."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _epoch_ms_to_utc(value) -> datetime | None:
    """Epoch milliseconds -> tz-aware UTC datetime; None for non-numeric/non-finite/out-of-range."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _jobs_list(payload) -> list:
    """Greenhouse/Ashby wrap jobs under a key; Lever returns a bare list. Accept 'jobs'
    or (Ashby-style) 'results'; anything else yields an empty list."""
    if isinstance(payload, dict):
        for key in ("jobs", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []
    if isinstance(payload, list):
        return payload
    return []


def parse_greenhouse(payload: dict, company: str) -> list[JobRecord]:
    out: list[JobRecord] = []
    for job in _jobs_list(payload):
        if not isinstance(job, dict):
            continue
        loc_obj = job.get("location")
        location = loc_obj.get("name") if isinstance(loc_obj, dict) else loc_obj
        rec = _record(
            company=company,
            source="greenhouse",
            title=job.get("title"),
            url=job.get("absolute_url"),
            external_id=job.get("id"),
            location=location,
            description=job.get("content"),
            posted_at=_parse_iso(job.get("updated_at")),
        )
        if rec:
            out.append(rec)
    return out


def parse_lever(payload: list[dict], company: str) -> list[JobRecord]:
    out: list[JobRecord] = []
    for job in _jobs_list(payload):
        if not isinstance(job, dict):
            continue
        categories = job.get("categories")
        location = categories.get("location") if isinstance(categories, dict) else None
        rec = _record(
            company=company,
            source="lever",
            title=job.get("text"),
            url=job.get("hostedUrl"),
            external_id=job.get("id"),
            location=location,
            description=job.get("descriptionPlain"),
            posted_at=_epoch_ms_to_utc(job.get("createdAt")),
        )
        if rec:
            out.append(rec)
    return out


def parse_ashby(payload: dict, company: str) -> list[JobRecord]:
    out: list[JobRecord] = []
    for job in _jobs_list(payload):
        if not isinstance(job, dict):
            continue
        rec = _record(
            company=company,
            source="ashby",
            title=job.get("title"),
            url=job.get("jobUrl"),
            external_id=job.get("id"),
            location=job.get("location"),
            description=job.get("descriptionPlain"),
            posted_at=_parse_iso(job.get("publishedAt")),
        )
        if rec:
            out.append(rec)
    return out
