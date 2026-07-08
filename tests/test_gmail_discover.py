from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.discovery.gmail_source import discover_gmail_alerts

FIXTURE = (Path(__file__).parent / "fixtures" / "linkedin_alert.eml").read_bytes()


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def test_discover_ingests_six_link_only_rows(conn):
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    report = discover_gmail_alerts(conn, fetch=lambda: [FIXTURE], now=now)
    assert report["messages"] == 1 and report["parsed"] == 6 and report["errors"] == []
    rows = conn.execute(
        "select source, term, description from jobs order by title"
    ).fetchall()
    assert len(rows) == 6
    assert all(r[0] == "gmail:linkedin-alert" for r in rows)
    assert all(r[1] == ["software engineer intern"] for r in rows)   # term stored
    assert all(r[2] is None for r in rows)                           # link-only (no JD)


def test_discover_dedupes_same_email_across_runs(conn):
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    discover_gmail_alerts(conn, fetch=lambda: [FIXTURE], now=now)
    discover_gmail_alerts(conn, fetch=lambda: [FIXTURE, FIXTURE], now=now)  # re-read: idempotent
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 6


def test_discover_is_failsoft_on_a_bad_message(conn):
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    # one good message + one junk message → the good one still ingests, the bad one is recorded
    report = discover_gmail_alerts(conn, fetch=lambda: [FIXTURE, b"junk"], now=now)
    assert report["messages"] == 2
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 6
