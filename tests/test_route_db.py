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


def _insert(conn, *, title, description, dedupe_key, resume_type=None, route_method=None, enrich_attempts=0):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, "
        "route_method, enrich_attempts) values (%s, 'github:simplify', 'Acme', %s, %s, %s, %s, %s, %s)",
        (dedupe_key, title, f"https://x/{dedupe_key}", description, resume_type, route_method, enrich_attempts),
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


def _fake_llm_ai(task, messages, **kw):
    return '{"type": "ai", "confidence": 0.9}'


def test_route_new_title_routes_exhausted_ambiguous_no_jd(conn):
    _insert(conn, title="AI Engineer / ML Engineer Intern", description=None,
            dedupe_key="t|amb", enrich_attempts=3)
    counts = route_new(conn, config=CONFIG, llm_complete=_fake_llm_ai)
    assert counts["llm_title"] == 1
    row = conn.execute(
        "select resume_type, route_method, route_confidence, status from jobs where dedupe_key='t|amb'"
    ).fetchone()
    assert row[0] == "ai" and row[1] == "llm_title" and row[2] <= 0.4 and row[3] == "routed"


def test_route_new_leaves_not_yet_exhausted_deferred(conn):
    _insert(conn, title="AI Engineer / ML Engineer Intern", description=None,
            dedupe_key="t|fresh", enrich_attempts=0)
    counts = route_new(conn, config=CONFIG, llm_complete=_fake_llm_ai)
    assert counts["deferred"] == 1 and counts["llm_title"] == 0
    assert conn.execute("select resume_type from jobs where dedupe_key='t|fresh'").fetchone()[0] is None


def test_route_new_not_target_is_marked_and_not_reselected(conn):
    # exhausted, no rules signal, non-internship title -> not_target (no LLM)
    _insert(conn, title="Senior Director of Finance", description=None,
            dedupe_key="t|nt", enrich_attempts=3)

    def _llm_never(*a, **k):
        raise AssertionError("LLM must not be called for a non-internship not_target")

    counts1 = route_new(conn, config=CONFIG, llm_complete=_llm_never)
    assert counts1["not_target"] == 1
    row = conn.execute("select resume_type, route_method from jobs where dedupe_key='t|nt'").fetchone()
    assert row == (None, "not_target")
    # second run must NOT reselect it
    counts2 = route_new(conn, config=CONFIG, llm_complete=_llm_never)
    assert counts2["not_target"] == 0 and counts2["deferred"] == 0


def test_title_routing_uses_separate_budget_not_jd_budget(conn):
    # one exhausted-no-JD ambiguous row + assert the JD max_llm_calls budget is not consumed by it
    _insert(conn, title="AI Engineer / ML Engineer Intern", description=None,
            dedupe_key="t|sep", enrich_attempts=3)
    # max_llm_calls=0 would block JD routing, but title routing has its own budget -> still routes
    counts = route_new(conn, config=CONFIG, llm_complete=_fake_llm_ai, max_llm_calls=0)
    assert counts["llm_title"] == 1
