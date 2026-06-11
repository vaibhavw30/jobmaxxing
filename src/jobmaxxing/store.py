import psycopg
from psycopg.rows import dict_row

from .merge import merge_records
from .models import JobRecord

_INSERT_COLS = (
    "dedupe_key, source, external_id, company, title, location, "
    "url, alt_urls, description, posted_at, is_active"
)
_INSERT_SQL = (
    f"insert into jobs ({_INSERT_COLS}) "
    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "on conflict (dedupe_key) do nothing"
)
# company/title are intentionally NOT in the UPDATE: they define the dedupe_key, so
# changing them would make it a different job. Only enrichable fields are refreshed.
_UPDATE_SQL = (
    "update jobs set source=%s, external_id=%s, location=%s, url=%s, "
    "alt_urls=%s, description=%s, posted_at=%s, is_active=%s, scraped_at=now() "
    "where dedupe_key=%s"
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


def upsert_jobs(conn: psycopg.Connection, records: list[JobRecord]) -> dict[str, int]:
    """Insert new rows; on dedupe_key conflict, lock the row, merge, and update.

    Race-free upsert: INSERT ... ON CONFLICT DO NOTHING claims the row. If a concurrent
    poller already inserted the same dedupe_key, our insert affects 0 rows and we fall
    through to the merge path, where SELECT ... FOR UPDATE blocks until the other
    transaction commits, so we always merge against the committed row. Each record is
    its own transaction, so overlapping GitHub Actions runs cannot duplicate or corrupt.

    The whole batch is validated up front: a record with an empty dedupe_key raises
    ValueError before anything is written (an empty key would collapse unrelated rows
    under the unique constraint). Up-front validation avoids leaving a partial commit.
    """
    for rec in records:
        if not rec.dedupe_key:
            raise ValueError(
                f"refusing to upsert record with empty dedupe_key: "
                f"{rec.company!r} / {rec.title!r} ({rec.source})"
            )

    counts = {"inserted": 0, "merged": 0}
    cur = conn.cursor(row_factory=dict_row)
    for rec in records:
        with conn.transaction():
            cur.execute(_INSERT_SQL, _record_values(rec))
            if cur.rowcount == 1:
                counts["inserted"] += 1
                continue
            # Conflict: the row already exists. Lock it, merge, and update.
            existing = cur.execute(
                "select * from jobs where dedupe_key = %s for update",
                (rec.dedupe_key,),
            ).fetchone()
            merged = merge_records(_row_to_record(existing), rec)
            cur.execute(
                _UPDATE_SQL,
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
