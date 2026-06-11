from datetime import datetime, timezone

from ..models import JobRecord
from ..normalize import make_dedupe_key


def parse_simplify_format(payload: list[dict], source: str) -> list[JobRecord]:
    """Parse a Simplify-format listings.json (Simplify / vanshb03 / pitt-csc forks).

    Skips entries missing company_name, title, or url.
    """
    records: list[JobRecord] = []
    for entry in payload:
        company = entry.get("company_name")
        title = entry.get("title")
        url = entry.get("url")
        if not company or not title or not url:
            continue

        locations = entry.get("locations") or []
        location = ", ".join(locations) if locations else None

        posted_at = None
        epoch = entry.get("date_posted")
        if epoch:
            posted_at = datetime.fromtimestamp(epoch, tz=timezone.utc)

        records.append(
            JobRecord(
                source=source,
                company=company,
                title=title,
                url=url,
                external_id=entry.get("id"),
                location=location,
                posted_at=posted_at,
                is_active=bool(entry.get("active", True)),
                dedupe_key=make_dedupe_key(company, title),
            )
        )
    return records
