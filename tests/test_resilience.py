from datetime import datetime, timezone

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.models import JobRecord
from jobmaxxing.run import run_sources


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _good_source():
    return [
        JobRecord(source="github:simplify", company="Acme", title="SWE Intern",
                  url="https://x/1", dedupe_key="acme|swe intern")
    ]


def _broken_source():
    raise RuntimeError("source is down")


def test_run_sources_isolates_failures(conn):
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    report = run_sources(
        conn,
        sources=[("broken", _broken_source), ("good", _good_source)],
        now=now,
    )
    # broken source recorded as failed, good source still ingested
    assert report["broken"]["status"] == "failed"
    assert "source is down" in report["broken"]["error"]
    assert report["good"]["status"] == "ok"
    assert report["good"]["inserted"] == 1
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 1
