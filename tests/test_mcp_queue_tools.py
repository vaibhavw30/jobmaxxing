import uuid

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.mcp.tools import nightly_queue


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _insert(conn, *, dedupe_key, description="", resume_type="swe", route_method="rules",
            recover_attempts=0, jd_source=None, company="Acme", title="SWE Intern"):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, "
        "route_method, recover_attempts, jd_source) "
        "values (%s,'github:simplify',%s,%s,%s,%s,%s,%s,%s,%s)",
        (dedupe_key, company, title, f"https://x/{dedupe_key}", description, resume_type,
         route_method, recover_attempts, jd_source),
    )
    conn.commit()
    return conn.execute("select id from jobs where dedupe_key=%s", (dedupe_key,)).fetchone()[0]


def test_nightly_queue_selects_only_exhausted_relevant_jdless(conn):
    _insert(conn, dedupe_key="q|ok", recover_attempts=2)                                 # appears
    _insert(conn, dedupe_key="q|fresh", recover_attempts=1)                              # not exhausted
    _insert(conn, dedupe_key="q|hasdesc", description="already", recover_attempts=2)     # has JD
    _insert(conn, dedupe_key="q|norel", resume_type=None, route_method=None, recover_attempts=2)  # not relevant
    _insert(conn, dedupe_key="q|manual", route_method="manual", recover_attempts=2)      # manual
    rows = nightly_queue(conn)
    assert {r["title"] for r in rows} == {"SWE Intern"}
    assert len(rows) == 1
    r = rows[0]
    assert r["resume_type"] == "swe" and r["url"].endswith("q|ok")
    assert isinstance(r["id"], str)        # JSON-safe (uuid -> str)


def test_nightly_queue_caps_limit(conn):
    for i in range(3):
        _insert(conn, dedupe_key=f"q|c{i}", recover_attempts=2)
    assert len(nightly_queue(conn, limit=2)) == 2
