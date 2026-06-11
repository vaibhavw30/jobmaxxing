from datetime import datetime, timezone

from ..models import JobRecord
from ..normalize import make_dedupe_key


def _record(company, source, title, url, external_id, location, description, posted_at):
    if not title or not url:
        return None
    return JobRecord(
        source=source,
        company=company,
        title=title,
        url=url,
        external_id=str(external_id) if external_id is not None else None,
        location=location,
        description=description,
        posted_at=posted_at,
        is_active=True,
        dedupe_key=make_dedupe_key(company, title),
    )


def _parse_iso(value) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None for missing/non-string/unparseable values."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _jobs_list(payload) -> list:
    """Greenhouse/Ashby wrap jobs under {"jobs": [...]}; Lever returns a bare list."""
    if isinstance(payload, dict):
        jobs = payload.get("jobs")
        return jobs if isinstance(jobs, list) else []
    if isinstance(payload, list):
        return payload
    return []


def parse_greenhouse(payload: dict, company: str) -> list[JobRecord]:
    out: list[JobRecord] = []
    for job in _jobs_list(payload):
        if not isinstance(job, dict):
            continue
        loc_obj = job.get("location")
        if isinstance(loc_obj, dict):
            location = loc_obj.get("name")
        elif isinstance(loc_obj, str):
            location = loc_obj
        else:
            location = None
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
        posted_at = None
        created = job.get("createdAt")
        # epoch milliseconds; bool is a subclass of int, exclude it.
        if isinstance(created, (int, float)) and not isinstance(created, bool) and created > 0:
            posted_at = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
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
            posted_at=posted_at,
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
