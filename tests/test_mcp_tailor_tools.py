import json

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.mcp.tools import get_review, tailor
from jobmaxxing.tailoring.latex import CompileResult
from jobmaxxing.tailoring.storage import ArtifactMissing, InMemoryStore


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


RUBRIC = {"keyword_dict": ["python"], "aliases": {}}


def _insert_approved(conn):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, status) "
        "values ('a|x', 'github:simplify', 'Acme', 'SWE', 'https://x', 'needs python', 'swe', "
        "'approved_for_tailoring')"
    )
    conn.commit()
    return conn.execute("select id from jobs").fetchone()[0]


def _fake_complete(task, messages, **kw):
    if task == "review" and kw.get("response_format"):
        return '{"weaknesses": ["w1", "w2", "w3"], "missing_keywords": ["kafka"]}'
    return r"\documentclass{article}\begin{document} python \end{document}"


def _fake_compile(tex):
    return CompileResult(pdf_bytes=b"%PDF fake", page_count=1, log="")


def test_tailor_runs_loop_and_returns_review(conn, monkeypatch):
    import jobmaxxing.mcp.tools as tools_mod

    monkeypatch.setattr(tools_mod, "load_rubric", lambda t: RUBRIC)
    job_id = _insert_approved(conn)
    store = InMemoryStore(base_resumes={"swe": r"\documentclass{article} base"})

    review = tailor(conn, job_id, store=store, complete=_fake_complete, compile_fn=_fake_compile)
    assert review["missing_keywords"] == ["kafka"]
    assert conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()[0] == "tailored"


def test_get_review_roundtrips_artifacts():
    store = InMemoryStore()
    store.put_artifact("job1", "review.json", json.dumps({"score_after": {"static": 1.0}}).encode())
    store.put_artifact("job1", "diff.txt", b"--- base\n+++ tailored\n")
    out = get_review(store, "job1")
    assert out["review"]["score_after"]["static"] == 1.0
    assert out["diff"].startswith("--- base")


def test_get_review_missing_raises():
    with pytest.raises(ArtifactMissing):
        get_review(InMemoryStore(), "nope")
