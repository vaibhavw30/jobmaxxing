import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.mcp.tools import approve, set_status


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _insert(conn, status="routed"):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, status) "
        "values ('a|x', 'github:simplify', 'Acme', 'SWE', 'https://x', %s)",
        (status,),
    )
    conn.commit()
    return conn.execute("select id from jobs").fetchone()[0]


def test_approve_sets_status(conn):
    job_id = _insert(conn)
    out = approve(conn, job_id)
    assert out["status"] == "approved_for_tailoring"
    assert conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()[0] == "approved_for_tailoring"


def test_set_status_valid_transition(conn):
    job_id = _insert(conn, status="tailored")
    out = set_status(conn, job_id, "applied")
    assert out["status"] == "applied"
    assert conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()[0] == "applied"


def test_set_status_rejects_unknown_status(conn):
    job_id = _insert(conn)
    with pytest.raises(ValueError):
        set_status(conn, job_id, "bogus")


def test_set_status_missing_job_raises(conn):
    with pytest.raises(ValueError):
        set_status(conn, "00000000-0000-0000-0000-000000000000", "applied")
