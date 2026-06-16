from .models import JobRecord
from .normalize import ATS_SOURCES


def _is_ats(source: str) -> bool:
    # ATS classification keys off ATS_SOURCES (normalize.py). When adding a new ATS
    # poller, add its source prefix there or it will be treated as a listing scraper.
    return source.split(":", 1)[0] in ATS_SOURCES


def merge_records(existing: JobRecord, incoming: JobRecord) -> JobRecord:
    """Combine an existing row with a newly-seen duplicate. Never drops a URL.

    A "primary" record supplies identity (url, source, company, title) and is preferred
    for every field; the "secondary" only fills nullable gaps. An ATS source outranks a
    non-ATS one for primacy, so a direct ATS hit upgrades a listing-scraped row (richer
    URL, full-JD text, stable external_id). Otherwise the existing row stays primary.

    - url / source / company / title / posted_at: from primary.
    - description / external_id / location: primary's value, else secondary's (fill-when-null).
    - alt_urls: every other url seen, order-preserving dedup, excluding the canonical url.
    - is_active: from incoming (the most recent observation).
    - dedupe_key: preserved from existing, falling back to incoming.
    - term: primary's value, else secondary's (None-aware fill; preserves an empty-list marker).

    Note: posted_at is taken from primary, so an ATS promotion refreshes the date, but a
    non-primary record never overwrites the primary's date (conservative — avoids a
    less-authoritative source clobbering a good date). Email/link-only rows are not a
    source in this sprint, so stale-date freezing is not a live risk here.
    """
    if _is_ats(incoming.source) and not _is_ats(existing.source):
        primary, secondary = incoming, existing
    else:
        primary, secondary = existing, incoming

    seen = [*existing.alt_urls, *incoming.alt_urls, existing.url, incoming.url]
    alt_urls = [u for u in dict.fromkeys(seen) if u != primary.url]

    return JobRecord(
        source=primary.source,
        company=primary.company,
        title=primary.title,
        url=primary.url,
        external_id=primary.external_id or secondary.external_id,
        location=primary.location or secondary.location,
        description=primary.description or secondary.description,
        posted_at=primary.posted_at or secondary.posted_at,
        # is_active reflects the most recent observation; an ATS poller seeing a role as
        # open re-activates a row a list had marked closed. ATS boards list only open roles.
        is_active=incoming.is_active,
        alt_urls=alt_urls,
        dedupe_key=existing.dedupe_key or incoming.dedupe_key,
        # term: primary's value, else secondary's. None-aware (not `or`) so an empty list — a real
        # "processed untagged" marker — is preserved, and a legacy NULL gets the fresh term.
        term=primary.term if primary.term is not None else secondary.term,
    )
