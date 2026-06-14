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


def _update_values(rec: JobRecord) -> tuple:
    """Values for _UPDATE_SQL (the enrichable columns + the dedupe_key WHERE)."""
    return (
        rec.source,
        rec.external_id,
        rec.location,
        rec.url,
        rec.alt_urls,
        rec.description,
        rec.posted_at,
        rec.is_active,
        rec.dedupe_key,
    )


def upsert_jobs(conn: psycopg.Connection, records: list[JobRecord]) -> dict[str, int]:
    """Insert new rows; on dedupe_key conflict, merge and update. Batched.

    Validates the whole batch up front (empty dedupe_key raises before any write). Then,
    in one transaction: bulk-INSERT the rows whose dedupe_key does not yet exist (ON CONFLICT
    DO NOTHING as a race safety net) and bulk-UPDATE the existing ones with their merge.
    executemany pipelines the statements, so a 6k-row batch is a handful of round-trips, not
    thousands. Intra-batch duplicate dedupe_keys are folded with merge_records first.

    A write failure propagates (the whole batch rolls back, retried idempotently next run);
    the returned counts are only meaningful when the function returns normally.
    """
    for rec in records:
        if not rec.dedupe_key:
            raise ValueError(
                f"refusing to upsert record with empty dedupe_key: "
                f"{rec.company!r} / {rec.title!r} ({rec.source})"
            )
    if not records:
        return {"inserted": 0, "merged": 0}

    # Fold intra-batch duplicates so each dedupe_key appears once (later records enrich earlier).
    folded: dict[str, JobRecord] = {}
    for rec in records:
        folded[rec.dedupe_key] = (
            merge_records(folded[rec.dedupe_key], rec) if rec.dedupe_key in folded else rec
        )

    keys = list(folded)
    with conn.cursor(row_factory=dict_row) as cur:
        # Select only the columns _row_to_record reads — avoids hauling the unused
        # (and growing) score_before/after jsonb back over the pooler for every row.
        existing = {
            row["dedupe_key"]: row
            for row in cur.execute(
                "select dedupe_key, source, external_id, company, title, location, "
                "url, alt_urls, description, posted_at, is_active "
                "from jobs where dedupe_key = any(%s)",
                (keys,),
            ).fetchall()
        }

        to_insert = [_record_values(rec) for key, rec in folded.items() if key not in existing]
        to_update = [
            _update_values(merge_records(_row_to_record(existing[key]), rec))
            for key, rec in folded.items()
            if key in existing
        ]

        with conn.transaction():
            if to_insert:
                cur.executemany(_INSERT_SQL, to_insert)
            if to_update:
                cur.executemany(_UPDATE_SQL, to_update)

    return {"inserted": len(to_insert), "merged": len(to_update)}
