import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.routing.route import route_new, set_manual


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
    "thresholds": {"min_top_score": 1.0, "min_margin_ratio": 0.5, "jd_hits_cap": 5, "max_llm_calls_per_run": 50},
    "types": {
        "swe": {"definition": "x", "title_signals": ["software engineer"], "jd_signals": ["api"], "exclude_signals": []},
        "ai": {"definition": "x", "title_signals": ["ai engineer"], "jd_signals": ["llm"], "exclude_signals": []},
        "mle": {"definition": "x", "title_signals": ["ml engineer"], "jd_signals": ["training"], "exclude_signals": []},
    },
}


def _insert(conn, *, title, description, dedupe_key, resume_type=None, route_method=None):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, route_method) "
        "values (%s, 'github:simplify', 'Acme', %s, %s, %s, %s, %s)",
        (dedupe_key, title, f"https://x/{dedupe_key}", description, resume_type, route_method),
    )
    conn.commit()


def test_route_new_routes_clear_title_by_rules(conn):
    _insert(conn, title="Software Engineer Intern", description="api work", dedupe_key="a|swe")

    def llm_never(*a, **k):
        raise AssertionError("LLM should not be called")

    counts = route_new(conn, config=CONFIG, llm_complete=llm_never)
    assert counts["rules"] == 1 and counts["llm"] == 0
    row = conn.execute("select resume_type, route_method, status from jobs").fetchone()
    assert row == ("swe", "rules", "routed")


def test_route_new_uses_llm_for_ambiguous_with_jd(conn):
    _insert(conn, title="AI Engineer / ML Engineer Intern", description="generic body", dedupe_key="a|ai-mle")

    def fake_llm(task, messages, **kw):
        return '{"type": "ai", "confidence": 0.8}'

    counts = route_new(conn, config=CONFIG, llm_complete=fake_llm)
    assert counts["llm"] == 1
    row = conn.execute("select resume_type, route_method from jobs").fetchone()
    assert row == ("ai", "llm")


def test_route_new_defers_title_only_ambiguous(conn):
    _insert(conn, title="AI Engineer / ML Engineer Intern", description=None, dedupe_key="a|titleonly")

    def llm_never(*a, **k):
        raise AssertionError("LLM should not be called")

    counts = route_new(conn, config=CONFIG, llm_complete=llm_never)
    assert counts["deferred"] == 1
    row = conn.execute("select resume_type, status from jobs").fetchone()
    assert row == (None, "new")     # left for a later run


def test_route_new_skips_manual_rows(conn):
    _insert(conn, title="Software Engineer Intern", description="api", dedupe_key="a|manual",
            resume_type="quant-trader", route_method="manual")

    counts = route_new(conn, config=CONFIG, llm_complete=lambda *a, **k: "{}")
    assert counts["manual_skipped"] == 1          # the one manual row is reported, not re-routed
    assert counts["rules"] == 0 and counts["llm"] == 0
    row = conn.execute("select resume_type, route_method from jobs").fetchone()
    assert row == ("quant-trader", "manual")      # untouched


def test_route_new_is_idempotent(conn):
    _insert(conn, title="Software Engineer Intern", description="api", dedupe_key="a|idem")
    route_new(conn, config=CONFIG, llm_complete=lambda *a, **k: "{}")
    counts2 = route_new(conn, config=CONFIG, llm_complete=lambda *a, **k: "{}")
    assert counts2["rules"] == 0 and counts2["llm"] == 0   # already routed, nothing to do


def test_route_new_batches_mixed_rows(conn):
    # clear-title -> rules; title-only-ambiguous -> deferred; manual -> skipped
    _insert(conn, title="Software Engineer Intern", description="api work", dedupe_key="b|swe")
    _insert(conn, title="AI Engineer / ML Engineer Intern", description=None, dedupe_key="b|titleonly")
    _insert(conn, title="Quant Intern", description="api", dedupe_key="b|manual",
            resume_type="quant-trader", route_method="manual")

    counts = route_new(conn, config=CONFIG, llm_complete=lambda *a, **k: "{}")
    assert counts["rules"] == 1
    assert counts["deferred"] == 1
    assert counts["manual_skipped"] == 1
    rows = dict(conn.execute("select dedupe_key, status from jobs").fetchall())
    assert rows["b|swe"] == "routed"
    assert rows["b|titleonly"] == "new"
    by_method = dict(conn.execute("select dedupe_key, route_method from jobs").fetchall())
    assert by_method["b|swe"] == "rules"
    assert by_method["b|manual"] == "manual"


def test_set_manual_overrides_and_validates(conn):
    _insert(conn, title="Whatever", description=None, dedupe_key="a|set")
    job_id = conn.execute("select id from jobs").fetchone()[0]
    set_manual(conn, job_id, "fdse")
    row = conn.execute("select resume_type, route_method, route_confidence, status from jobs").fetchone()
    assert row[0] == "fdse" and row[1] == "manual" and row[2] == 1.0 and row[3] == "routed"
    with pytest.raises(ValueError):
        set_manual(conn, job_id, "not-a-type")
