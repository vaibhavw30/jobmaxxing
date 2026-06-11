import os

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations


@pytest.fixture
def conn(postgresql):
    # pytest-postgresql provides a fresh database via the `postgresql` fixture.
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        yield c


def test_apply_migrations_creates_jobs_table(conn):
    apply_migrations(conn)
    row = conn.execute("select count(*) from jobs").fetchone()
    assert row[0] == 0
    # view exists
    conn.execute("select * from active_unrouted").fetchall()
