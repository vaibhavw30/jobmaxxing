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


from jobmaxxing.mcp.tools import set_description


def test_set_description_writes_jd_and_resets_routing(conn):
    jid = _insert(conn, dedupe_key="sd|1", resume_type="mle", route_method="llm_title", recover_attempts=2)
    out = set_description(conn, jid, "  A real pasted job description with words.  ")
    assert out["jd_source"] == "manual" and out["chars"] == len("A real pasted job description with words.")
    row = conn.execute(
        "select description, jd_source, resume_type, route_method from jobs where id=%s", (jid,)
    ).fetchone()
    assert row[0] == "A real pasted job description with words."
    assert row[1] == "manual" and row[2] is None and row[3] is None     # reset so route_new re-routes


def test_set_description_rejects_empty(conn):
    jid = _insert(conn, dedupe_key="sd|2", recover_attempts=2)
    with pytest.raises(ValueError, match="empty"):
        set_description(conn, jid, "   ")


def test_set_description_unknown_id_raises(conn):
    with pytest.raises(ValueError, match="no job"):
        set_description(conn, uuid.uuid4(), "some jd")


from jobmaxxing.mcp.tools import reject_recovered


def _jid_in(rows, jid):
    return any(r["id"] == str(jid) for r in rows)


def test_reject_recovered_clears_jd_and_returns_to_queue(conn):
    jid = _insert(conn, dedupe_key="rr|1", description="wrong recovered jd", jd_source="recovered",
                  resume_type="swe", route_method="rules", recover_attempts=0)
    out = reject_recovered(conn, jid)
    assert out["status"] == "rejected_recovered"
    row = conn.execute(
        "select description, jd_source, resume_type, recover_attempts from jobs where id=%s", (jid,)
    ).fetchone()
    assert row[0] is None and row[1] is None and row[2] == "swe" and row[3] >= 2  # JD cleared, type kept, capped
    # and it now reappears in the nightly worklist for manual capture
    assert _jid_in(nightly_queue(conn), jid)


def test_reject_recovered_only_touches_recovered_rows(conn):
    jid = _insert(conn, dedupe_key="rr|2", description="an ATS jd", jd_source=None, recover_attempts=0)
    with pytest.raises(ValueError, match="no recovered job"):
        reject_recovered(conn, jid)
    # unchanged
    assert conn.execute("select description from jobs where id=%s", (jid,)).fetchone()[0] == "an ATS jd"


from jobmaxxing.mcp.tools import query_jobs


def test_query_jobs_filters_by_jd_source(conn):
    _insert(conn, dedupe_key="js|rec", description="d", jd_source="recovered")
    _insert(conn, dedupe_key="js|ats", description="d", jd_source=None)
    rows = query_jobs(conn, jd_source="recovered")
    assert len(rows) == 1 and rows[0]["url"].endswith("js|rec")
