import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def test_funnel_counts_view_queryable(conn):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, status) "
        "values ('a|x', 'github:simplify', 'Acme', 'SWE', 'https://x', 'tailored')"
    )
    conn.commit()
    rows = dict(conn.execute("select status, n from funnel_counts").fetchall())
    assert rows["tailored"] == 1


def test_review_queue_lists_tailored(conn):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, status, resume_type) "
        "values ('a|y', 'github:simplify', 'Acme', 'SWE', 'https://y', 'tailored', 'swe')"
    )
    conn.commit()
    row = conn.execute("select company, resume_type from review_queue").fetchone()
    assert row == ("Acme", "swe")
