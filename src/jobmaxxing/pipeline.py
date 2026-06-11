from dataclasses import replace
from datetime import datetime

import psycopg

from .models import JobRecord
from .normalize import canonicalize_url, within_age_cutoff
from .store import upsert_jobs


def _canonicalize(rec: JobRecord) -> JobRecord:
    """Return a copy with url and alt_urls canonicalized. Does not mutate the input."""
    return replace(
        rec,
        url=canonicalize_url(rec.url),
        alt_urls=[canonicalize_url(u) for u in rec.alt_urls],
    )


def ingest_records(conn: psycopg.Connection, records: list[JobRecord], now: datetime) -> dict:
    """Canonicalize URLs, apply the age cutoff, then upsert the survivors.

    Canonicalization happens here (the single chokepoint before storage) so tracking-param
    variants of the same link collapse in `url`/`alt_urls`.
    """
    fresh = [_canonicalize(r) for r in records if within_age_cutoff(r.posted_at, now)]
    skipped_old = len(records) - len(fresh)
    counts = upsert_jobs(conn, fresh)
    return {**counts, "skipped_old": skipped_old}
