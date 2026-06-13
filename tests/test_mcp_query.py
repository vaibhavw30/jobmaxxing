import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.mcp.tools import query_jobs


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _insert(conn, *, dedupe_key, company="Acme", status="new", resume_type=None):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, status, resume_type) "
        "values (%s, 'github:simplify', %s, 'SWE Intern', %s, %s, %s)",
        (dedupe_key, company, f"https://x/{dedupe_key}", status, resume_type),
    )
    conn.commit()


def test_query_returns_json_safe_rows(conn):
    _insert(conn, dedupe_key="a|1")
    rows = query_jobs(conn)
    assert len(rows) == 1
    assert isinstance(rows[0]["id"], str)            # uuid stringified
    assert rows[0]["company"] == "Acme" and rows[0]["status"] == "new"


def test_query_filters_by_status_and_type(conn):
    _insert(conn, dedupe_key="a|1", status="routed", resume_type="swe")
    _insert(conn, dedupe_key="a|2", status="new")
    assert len(query_jobs(conn, status="routed")) == 1
    assert query_jobs(conn, resume_type="swe")[0]["resume_type"] == "swe"


def test_query_company_is_case_insensitive_substring(conn):
    _insert(conn, dedupe_key="a|1", company="Acme Corp")
    assert len(query_jobs(conn, company="acme")) == 1


def test_query_limit_is_capped(conn):
    for i in range(5):
        _insert(conn, dedupe_key=f"a|{i}")
    assert len(query_jobs(conn, limit=2)) == 2
    assert len(query_jobs(conn, limit=10_000)) == 5      # hard cap doesn't error
