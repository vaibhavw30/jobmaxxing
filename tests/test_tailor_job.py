import json

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.tailoring.latex import CompileResult
from jobmaxxing.tailoring.storage import InMemoryStore
from jobmaxxing.tailoring.tailor import approve, tailor_job


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


RUBRIC = {"keyword_dict": ["kubernetes", "python"], "aliases": {"kubernetes": ["k8s"]}}


def _insert(conn, *, status, resume_type="swe", description="needs kubernetes and python"):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, status) "
        "values ('a|swe', 'github:simplify', 'Acme', 'SWE Intern', 'https://x', %s, %s, %s)",
        (description, resume_type, status),
    )
    conn.commit()
    return conn.execute("select id from jobs").fetchone()[0]


def _fake_complete(task, messages, **kw):
    # build/patch/shrink return tex; critique returns JSON
    if task == "review" and kw.get("response_format"):
        return '{"weaknesses": ["w1", "w2", "w3"], "missing_keywords": ["kafka"]}'
    return r"\documentclass{article}\begin{document} python kubernetes \end{document}"


def _fake_compile(tex):
    return CompileResult(pdf_bytes=b"%PDF-1.5 fake", page_count=1, log="")


def test_tailor_job_writes_artifacts_and_marks_tailored(conn):
    job_id = _insert(conn, status="approved_for_tailoring")
    store = InMemoryStore(base_resumes={"swe": r"\documentclass{article} base"})

    review = tailor_job(
        conn, job_id, store=store, complete=_fake_complete,
        compile_fn=_fake_compile, rubric_loader=lambda t: RUBRIC,
    )

    names = {name for (jid, name) in store.artifacts}
    assert names == {"tailored.tex", "tailored.pdf", "review.json", "diff.txt"}
    saved = json.loads(store.artifacts[(str(job_id), "review.json")])
    assert "static" in saved["score_after"] and saved["weaknesses"] == ["w1", "w2", "w3"]
    assert review["missing_keywords"] == ["kafka"]
    row = conn.execute("select status, artifact_prefix, score_after from jobs where id=%s", (job_id,)).fetchone()
    assert row[0] == "tailored"
    assert row[1] == store.artifact_prefix(job_id)
    assert row[2]["static"] == 1.0                  # tailored tex contains python + kubernetes


def test_tailor_job_refuses_unapproved(conn):
    job_id = _insert(conn, status="routed")
    store = InMemoryStore(base_resumes={"swe": "base"})
    with pytest.raises(ValueError):
        tailor_job(conn, job_id, store=store, complete=_fake_complete,
                   compile_fn=_fake_compile, rubric_loader=lambda t: RUBRIC)


def test_approve_sets_status(conn):
    job_id = _insert(conn, status="routed")
    approve(conn, job_id)
    assert conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()[0] == "approved_for_tailoring"


def _compile_two_pages(tex):
    return CompileResult(pdf_bytes=b"%PDF over", page_count=2, log="")


def test_tailor_job_persists_fit_false_when_never_one_page(conn):
    job_id = _insert(conn, status="approved_for_tailoring")
    store = InMemoryStore(base_resumes={"swe": r"\documentclass{article} base"})

    review = tailor_job(
        conn, job_id, store=store, complete=_fake_complete,
        compile_fn=_compile_two_pages, rubric_loader=lambda t: RUBRIC,
    )
    assert review["fit"] is False and review["retries"] == 3
    saved = json.loads(store.artifacts[(str(job_id), "review.json")])
    assert saved["fit"] is False
    # still recorded as tailored (operator reviews via review.json's fit flag)
    assert conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()[0] == "tailored"


def test_approve_re_approves_tailored_job(conn):
    job_id = _insert(conn, status="tailored")
    approve(conn, job_id)   # allowed (re-tailor) + warns; must not raise
    assert conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()[0] == "approved_for_tailoring"
