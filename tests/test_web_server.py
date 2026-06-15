"""Tests for src/jobmaxxing/web/server.py — Flask triage HTTP layer (Task 4).

Flask must be installed (uv sync --extra web).  If absent the whole module skips.
"""

import uuid

import psycopg
import pytest

flask = pytest.importorskip("flask")

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.web.server import create_app


# ---------------------------------------------------------------------------
# Fixtures & helpers (copied from test_web_triage.py — no conftest convention)
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _insert(conn, *, dedupe_key, resume_type="swe", status="routed", description="<p>jd</p>",
            company="Acme", title="SWE Intern", scraped_at=None):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, status"
        + (", scraped_at" if scraped_at is not None else "")
        + ") values (%s,'github:simplify',%s,%s,%s,%s,%s,%s"
        + (",%s" if scraped_at is not None else "")
        + ")",
        (dedupe_key, company, title, f"https://x/{dedupe_key}", description, resume_type, status)
        + ((scraped_at,) if scraped_at is not None else ()),
    )
    conn.commit()
    return str(conn.execute("select id from jobs where dedupe_key=%s", (dedupe_key,)).fetchone()[0])


# ---------------------------------------------------------------------------
# Connection wrapper: lets the test app share the test conn without closing it
# ---------------------------------------------------------------------------


class _KeepOpen:
    """Context manager that wraps a live psycopg conn without closing it on __exit__.

    The triage functions call conn.transaction() which commits; we must not
    double-commit or rollback, so __exit__ is a true no-op.
    """

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False  # no-op: do NOT close / commit / rollback


@pytest.fixture
def client(conn):
    app = create_app(conn_factory=lambda: _KeepOpen(conn))
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_renders_seeded_job(client, conn):
    """GET / returns 200 and includes the company name + a status badge."""
    _insert(conn, dedupe_key="ws|render", company="MegaCorp", status="routed")
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "MegaCorp" in body
    # status badge present
    assert "badge" in body
    assert "routed" in body


def test_post_decide_json_updates_db(client, conn):
    """POST /decide with JSON updates DB and returns JSON with status+changed."""
    jid = _insert(conn, dedupe_key="ws|decide", status="routed")
    resp = client.post("/decide", json={"job_id": jid, "interested": "yes"})
    assert resp.status_code == 200
    assert "application/json" in resp.content_type
    data = resp.get_json()
    assert "status" in data and "changed" in data
    assert data["status"] == "approved_for_tailoring"

    # re-query same conn to confirm DB write
    row = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid),)).fetchone()
    assert row[0] == "approved_for_tailoring"


def test_post_decide_non_json_415(client, conn):
    """POST /decide with form data returns 415."""
    jid = _insert(conn, dedupe_key="ws|415", status="routed")
    resp = client.post("/decide", data={"job_id": jid, "interested": "yes"})
    assert resp.status_code == 415


def test_post_reset_json(client, conn):
    """POST /reset reverts an approved job back to routed."""
    jid = _insert(conn, dedupe_key="ws|reset", status="approved_for_tailoring")
    resp = client.post("/reset", json={"job_id": jid})
    assert resp.status_code == 200
    assert "application/json" in resp.content_type

    row = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid),)).fetchone()
    assert row[0] == "routed"


def test_post_decide_unknown_id(client, conn):
    """POST /decide with unknown UUID returns 404."""
    fake_id = str(uuid.uuid4())
    resp = client.post("/decide", json={"job_id": fake_id, "interested": "yes"})
    assert resp.status_code == 404


def test_host_header_allowlist_rejects_foreign_host(client, conn):
    """Request with foreign Host header is rejected with 403."""
    resp = client.get("/", headers={"Host": "evil.com"})
    assert resp.status_code == 403


def test_two_sequential_decides_no_conn_leak(client, conn):
    """Two separate POST /decide calls both succeed and both writes are persisted."""
    jid_a = _insert(conn, dedupe_key="ws|seq|a", status="routed")
    jid_b = _insert(conn, dedupe_key="ws|seq|b", status="routed")

    resp_a = client.post("/decide", json={"job_id": jid_a, "interested": "yes"})
    assert resp_a.status_code == 200

    resp_b = client.post("/decide", json={"job_id": jid_b, "applied": "true"})
    assert resp_b.status_code == 200

    row_a = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid_a),)).fetchone()
    row_b = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid_b),)).fetchone()
    assert row_a[0] == "approved_for_tailoring"
    assert row_b[0] == "applied"


def test_favicon_no_db(client):
    """GET /favicon.ico returns 204 and does not touch the DB."""
    resp = client.get("/favicon.ico")
    assert resp.status_code == 204


def test_post_decide_malformed_json_400(client):
    """POST /decide with invalid JSON body returns 400, not 500."""
    resp = client.post("/decide", data="{not json", content_type="application/json")
    assert resp.status_code == 400


def test_post_decide_missing_job_id_400(client):
    """POST /decide with valid JSON but no job_id returns 400."""
    resp = client.post("/decide", json={"interested": "yes"})
    assert resp.status_code == 400


def test_get_default_excludes_decided(client, conn):
    """GET / with no query args shows only new/routed jobs; decided jobs are absent."""
    routed_id = _insert(conn, dedupe_key="ws|default|routed", status="routed")
    rejected_id = _insert(conn, dedupe_key="ws|default|rejected", status="rejected", resume_type="swe")

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert routed_id in body
    assert rejected_id not in body
