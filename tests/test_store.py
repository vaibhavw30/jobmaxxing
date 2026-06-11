import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.models import JobRecord
from jobmaxxing.store import upsert_jobs


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


def _rec(**kw):
    base = dict(source="github:simplify", company="Acme", title="SWE Intern", url="https://list/apply", dedupe_key="acme|swe intern")
    base.update(kw)
    return JobRecord(**base)


def test_upsert_inserts_new_row(conn):
    apply_migrations(conn)
    counts = upsert_jobs(conn, [_rec()])
    assert counts == {"inserted": 1, "merged": 0}
    row = conn.execute("select company, title, url from jobs").fetchone()
    assert row == ("Acme", "SWE Intern", "https://list/apply")


def test_upsert_merges_duplicate_and_enriches(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(source="github:simplify", url="https://list/apply", description=None)])
    counts = upsert_jobs(
        conn,
        [_rec(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1", external_id="gh-1", description="full JD")],
    )
    assert counts == {"inserted": 0, "merged": 1}
    row = conn.execute(
        "select count(*), max(url), max(description), max(external_id), max(array_to_string(alt_urls, ',')) from jobs"
    ).fetchone()
    assert row[0] == 1                                   # still one row
    assert row[1] == "https://boards.greenhouse.io/acme/jobs/1"  # ATS url promoted
    assert row[2] == "full JD"                           # description enriched
    assert row[3] == "gh-1"
    assert "https://list/apply" in row[4]                # old url preserved in alt_urls


def test_upsert_is_idempotent(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec()])
    upsert_jobs(conn, [_rec()])
    count = conn.execute("select count(*) from jobs").fetchone()[0]
    assert count == 1


def test_upsert_rejects_empty_dedupe_key(conn):
    apply_migrations(conn)
    with pytest.raises(ValueError):
        upsert_jobs(conn, [_rec(dedupe_key="")])
