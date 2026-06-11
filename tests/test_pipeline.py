from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.models import JobRecord
from jobmaxxing.pipeline import ingest_records


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _rec(title, posted_at):
    return JobRecord(
        source="github:simplify",
        company="Acme",
        title=title,
        url=f"https://x/{title}",
        posted_at=posted_at,
        dedupe_key=f"acme|{title.lower()}",
    )


def test_ingest_filters_old_and_upserts(conn):
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    records = [
        _rec("recent", now - timedelta(days=10)),
        _rec("ancient", now - timedelta(days=400)),
        _rec("undated", None),
    ]
    counts = ingest_records(conn, records, now=now)
    assert counts["inserted"] == 2          # recent + undated
    assert counts["skipped_old"] == 1
    titles = {r[0] for r in conn.execute("select title from jobs").fetchall()}
    assert titles == {"recent", "undated"}


def test_ingest_canonicalizes_urls_before_storing(conn):
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    rec = JobRecord(
        source="github:simplify",
        company="Acme",
        title="SWE Intern",
        url="https://simplify.jobs/p/abc?utm_source=x",
        alt_urls=["https://acme.com/careers/1/?ref=y"],
        posted_at=now,
        dedupe_key="acme|swe intern",
    )
    ingest_records(conn, [rec], now=now)
    row = conn.execute("select url, array_to_string(alt_urls, ',') from jobs").fetchone()
    assert row[0] == "https://simplify.jobs/p/abc"          # query stripped
    assert row[1] == "https://acme.com/careers/1"           # alt url canonicalized too


def test_ingest_does_not_mutate_input_records(conn):
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    rec = JobRecord(
        source="github:simplify",
        company="Acme",
        title="SWE Intern",
        url="https://x/a?utm=1",
        alt_urls=["https://y/b?ref=2"],
        posted_at=now,
        dedupe_key="acme|swe intern",
    )
    ingest_records(conn, [rec], now=now)
    # the pipeline must not mutate the caller's record in place
    assert rec.url == "https://x/a?utm=1"
    assert rec.alt_urls == ["https://y/b?ref=2"]
