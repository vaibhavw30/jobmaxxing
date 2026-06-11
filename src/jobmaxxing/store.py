import psycopg
from psycopg.rows import dict_row

from .merge import merge_records
from .models import JobRecord

_INSERT_COLS = (
    "dedupe_key, source, external_id, company, title, location, "
    "url, alt_urls, description, posted_at, is_active"
)


def _record_values(rec: JobRecord) -> tuple:
    return (
        rec.dedupe_key,
        rec.source,
        rec.external_id,
        rec.company,
        rec.title,
        rec.location,
        rec.url,
        rec.alt_urls,
        rec.description,
        rec.posted_at,
        rec.is_active,
    )


def _row_to_record(row: dict) -> JobRecord:
    return JobRecord(
        source=row["source"],
        company=row["company"],
        title=row["title"],
        url=row["url"],
        external_id=row["external_id"],
        location=row["location"],
        description=row["description"],
        posted_at=row["posted_at"],
        is_active=row["is_active"],
        alt_urls=list(row["alt_urls"]),
        dedupe_key=row["dedupe_key"],
    )


def upsert_jobs(conn: psycopg.Connection, records: list[JobRecord]) -> dict:
    """Insert new rows; for dedupe_key conflicts, lock the row, merge, and update.

    Per-record transaction with SELECT ... FOR UPDATE makes overlapping pollers safe.
    An empty dedupe_key is rejected: it would collapse unrelated rows under the
    unique (dedupe_key) constraint.
    """
    counts = {"inserted": 0, "merged": 0}
    for rec in records:
        if not rec.dedupe_key:
            raise ValueError(
                f"refusing to upsert record with empty dedupe_key: "
                f"{rec.company!r} / {rec.title!r} ({rec.source})"
            )
        with conn.transaction():
            existing = conn.cursor(row_factory=dict_row).execute(
                "select * from jobs where dedupe_key = %s for update",
                (rec.dedupe_key,),
            ).fetchone()

            if existing is None:
                conn.execute(
                    f"insert into jobs ({_INSERT_COLS}) values "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    _record_values(rec),
                )
                counts["inserted"] += 1
            else:
                merged = merge_records(_row_to_record(existing), rec)
                conn.execute(
                    "update jobs set source=%s, external_id=%s, location=%s, "
                    "url=%s, alt_urls=%s, description=%s, posted_at=%s, is_active=%s "
                    "where dedupe_key=%s",
                    (
                        merged.source,
                        merged.external_id,
                        merged.location,
                        merged.url,
                        merged.alt_urls,
                        merged.description,
                        merged.posted_at,
                        merged.is_active,
                        merged.dedupe_key,
                    ),
                )
                counts["merged"] += 1
    return counts
