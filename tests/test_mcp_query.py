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


def test_query_since_days_filters_old_rows(conn):
    # a fresh row (scraped_at defaults to now) is within 1 day; force an old row and exclude it
    _insert(conn, dedupe_key="a|recent")
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, status, scraped_at) "
        "values ('a|old', 'github:simplify', 'Acme', 'SWE', 'https://old', 'new', now() - interval '30 days')"
    )
    conn.commit()
    keys = {r["url"] for r in query_jobs(conn, since_days=1)}
    assert "https://x/a|recent" in keys
    assert "https://old" not in keys


def test_query_renders_null_route_confidence_as_none(conn):
    _insert(conn, dedupe_key="a|1")          # route_confidence left NULL
    assert query_jobs(conn)[0]["route_confidence"] is None


def test_query_empty_string_status_filters_to_zero(conn):
    _insert(conn, dedupe_key="a|1", status="new")
    assert query_jobs(conn, status="") == []     # explicit empty filter -> no rows, not all rows
