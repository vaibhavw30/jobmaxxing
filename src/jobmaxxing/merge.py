from .models import JobRecord
from .normalize import ATS_SOURCES


def _is_ats(source: str) -> bool:
    return source.split(":", 1)[0] in ATS_SOURCES


def merge_records(existing: JobRecord, incoming: JobRecord) -> JobRecord:
    """Combine an existing row with a newly-seen duplicate. Never drops a URL.

    Rules:
    - canonical url: prefer an ATS source url over a non-ATS one; otherwise keep existing.
    - description / external_id / location / posted_at: fill only when existing is null.
    - alt_urls: every other url seen, deduped, excluding the chosen canonical url.
    - is_active: refreshed from the incoming (most recent) row.
    """
    incoming_ats = _is_ats(incoming.source)
    existing_ats = _is_ats(existing.source)

    if incoming_ats and not existing_ats:
        canonical_url = incoming.url
        source = incoming.source
    else:
        canonical_url = existing.url
        source = existing.source

    seen = [*existing.alt_urls, *incoming.alt_urls, existing.url, incoming.url]
    alt_urls = [u for u in dict.fromkeys(seen) if u != canonical_url]

    return JobRecord(
        source=source,
        company=existing.company,
        title=existing.title,
        url=canonical_url,
        external_id=existing.external_id or incoming.external_id,
        location=existing.location or incoming.location,
        description=existing.description or incoming.description,
        posted_at=existing.posted_at or incoming.posted_at,
        is_active=incoming.is_active,
        alt_urls=alt_urls,
        dedupe_key=existing.dedupe_key,
    )
