import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.mcp.tools import preview_route, set_route


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


CONFIG = {
    "weights": {"title": 3.0, "jd": 1.0},
    "thresholds": {"min_top_score": 1.0, "min_margin_ratio": 0.5, "jd_hits_cap": 5},
    "types": {
        "swe": {"definition": "x", "title_signals": ["software engineer"], "jd_signals": ["api"], "exclude_signals": []},
    },
}


def _insert(conn, *, title="Software Engineer Intern", description="api work",
            resume_type="swe", route_method="rules", route_confidence=0.8):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, "
        "resume_type, route_method, route_confidence) "
        "values ('a|x', 'github:simplify', 'Acme', %s, 'https://x', %s, %s, %s, %s)",
        (title, description, resume_type, route_method, route_confidence),
    )
    conn.commit()
    return conn.execute("select id from jobs").fetchone()[0]


def test_preview_route_returns_stored(conn):
    job_id = _insert(conn)
    out = preview_route(conn, job_id)
    assert out["stored"] == {"resume_type": "swe", "route_method": "rules", "route_confidence": 0.8}
    assert "rerun" not in out


def test_preview_route_rerun_returns_live_decision_without_persisting(conn):
    job_id = _insert(conn, resume_type=None, route_method=None, route_confidence=None)

    def llm_never(*a, **k):
        raise AssertionError("clear title should not call the LLM")

    out = preview_route(conn, job_id, rerun=True, config=CONFIG, llm_complete=llm_never)
    assert out["rerun"]["resume_type"] == "swe" and out["rerun"]["method"] == "rules"
    # not persisted: the stored value is still null
    assert conn.execute("select resume_type from jobs where id=%s", (job_id,)).fetchone()[0] is None


def test_preview_route_missing_job_raises(conn):
    with pytest.raises(ValueError):
        preview_route(conn, "00000000-0000-0000-0000-000000000000")


def test_set_route_overrides_and_marks_manual(conn):
    job_id = _insert(conn, resume_type="swe")
    out = set_route(conn, job_id, "mle")
    assert out["resume_type"] == "mle" and out["route_method"] == "manual"
    row = conn.execute("select resume_type, route_method from jobs where id=%s", (job_id,)).fetchone()
    assert row == ("mle", "manual")


def test_set_route_invalid_type_raises(conn):
    job_id = _insert(conn)
    with pytest.raises(ValueError):
        set_route(conn, job_id, "not-a-type")
