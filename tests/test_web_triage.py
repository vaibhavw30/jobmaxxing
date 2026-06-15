"""Tests for src/jobmaxxing/web/triage.py — triage DB layer (Task 3)."""

import uuid

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.web.triage import apply_decision, fetch_triage_rows, reset_to_routed


# ---------------------------------------------------------------------------
# Fixtures & helpers (copied verbatim from test_sheet_sync.py with scraped_at kwarg added)
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
# fetch_triage_rows tests
# ---------------------------------------------------------------------------


def test_fetch_returns_routed_only_plain_jd(conn):
    """Seed a routed job and an unrouted job; only routed is returned; description is plain text."""
    jid = _insert(conn, dedupe_key="f|routed", description="<p>jd</p>")
    _insert(conn, dedupe_key="f|unrouted", resume_type=None)
    rows = fetch_triage_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert str(row["id"]) == jid
    assert row["description"] == "jd"
    # rows are dicts keyed by column names
    assert isinstance(row, dict)
    assert "company" in row and "title" in row and "status" in row


def test_fetch_filters_by_status_and_resume_type(conn):
    """Filters status= and resume_type= narrow results correctly."""
    a = _insert(conn, dedupe_key="f|s|routed|swe", status="routed", resume_type="swe")
    b = _insert(conn, dedupe_key="f|s|rejected|swe", status="rejected", resume_type="swe")
    c = _insert(conn, dedupe_key="f|s|routed|pm", status="routed", resume_type="pm")

    routed_rows = fetch_triage_rows(conn, status="routed")
    routed_ids = {str(r["id"]) for r in routed_rows}
    assert a in routed_ids
    assert c in routed_ids
    assert b not in routed_ids

    swe_rows = fetch_triage_rows(conn, resume_type="swe")
    swe_ids = {str(r["id"]) for r in swe_rows}
    assert a in swe_ids
    assert b in swe_ids
    assert c not in swe_ids

    both_rows = fetch_triage_rows(conn, status="routed", resume_type="swe")
    both_ids = {str(r["id"]) for r in both_rows}
    assert both_ids == {a}


def test_fetch_default_excludes_decided(conn):
    """No status filter returns ALL routed jobs regardless of status — filter boundary is in server layer."""
    a = _insert(conn, dedupe_key="f|d|routed", status="routed")
    b = _insert(conn, dedupe_key="f|d|applied", status="applied", resume_type="swe")
    c = _insert(conn, dedupe_key="f|d|rejected", status="rejected", resume_type="swe")
    d = _insert(conn, dedupe_key="f|d|tailored", status="tailored", resume_type="swe")
    all_rows = fetch_triage_rows(conn)
    all_ids = {str(r["id"]) for r in all_rows}
    # All have resume_type set, so all four are returned when no status filter is applied
    assert {a, b, c, d} == all_ids


def test_fetch_orders_newest_first(conn):
    """Explicit decreasing scraped_at values produce newest-first ordering."""
    from datetime import datetime, timezone
    t1 = datetime(2025, 1, 3, tzinfo=timezone.utc)
    t2 = datetime(2025, 1, 2, tzinfo=timezone.utc)
    t3 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a = _insert(conn, dedupe_key="f|o|newest", scraped_at=t1)
    b = _insert(conn, dedupe_key="f|o|middle", scraped_at=t2)
    c = _insert(conn, dedupe_key="f|o|oldest", scraped_at=t3)
    rows = fetch_triage_rows(conn)
    ids = [str(r["id"]) for r in rows]
    assert ids == [a, b, c]


def test_fetch_limit_capped(conn):
    """limit= is honoured and capped at _MAX_LIMIT (200); limit=0 is clamped to 1."""
    from jobmaxxing.web.triage import _MAX_LIMIT

    # Seed 3 routed jobs.
    _insert(conn, dedupe_key="lc|1")
    _insert(conn, dedupe_key="lc|2")
    _insert(conn, dedupe_key="lc|3")

    # limit=2 returns exactly 2 (cap is honoured when below max).
    rows_2 = fetch_triage_rows(conn, limit=2)
    assert len(rows_2) == 2

    # limit=500 exceeds _MAX_LIMIT but doesn't error; returns all 3 (< cap).
    rows_all = fetch_triage_rows(conn, limit=500)
    assert len(rows_all) == 3

    # limit=0 is clamped to 1, returns at least 1 row.
    rows_0 = fetch_triage_rows(conn, limit=0)
    assert len(rows_0) >= 1


# ---------------------------------------------------------------------------
# apply_decision tests
# ---------------------------------------------------------------------------


def test_apply_interested_yes_approves(conn):
    """interested='yes' on a routed job → approved_for_tailoring."""
    jid = _insert(conn, dedupe_key="a|yes")
    result = apply_decision(conn, jid, interested="yes")
    assert result["status"] == "approved_for_tailoring"
    assert result["changed"] is True
    assert str(result["job_id"]) == jid
    # Re-query to confirm DB state
    row = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid),)).fetchone()
    assert row[0] == "approved_for_tailoring"


def test_apply_no_rejects(conn):
    """interested='no' on a routed job → rejected."""
    jid = _insert(conn, dedupe_key="a|no")
    result = apply_decision(conn, jid, interested="no")
    assert result["status"] == "rejected"
    assert result["changed"] is True
    row = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid),)).fetchone()
    assert row[0] == "rejected"


def test_apply_applied_wins(conn):
    """applied='true' → status applied regardless of interested."""
    jid = _insert(conn, dedupe_key="a|applied")
    result = apply_decision(conn, jid, applied="true")
    assert result["status"] == "applied"
    assert result["changed"] is True
    row = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid),)).fetchone()
    assert row[0] == "applied"


def test_apply_idempotent(conn):
    """Second identical call returns changed:False, status unchanged."""
    jid = _insert(conn, dedupe_key="a|idempotent")
    apply_decision(conn, jid, interested="yes")
    result2 = apply_decision(conn, jid, interested="yes")
    assert result2["changed"] is False
    assert result2["status"] == "approved_for_tailoring"
    row = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid),)).fetchone()
    assert row[0] == "approved_for_tailoring"


def test_apply_no_regress_from_tailored(conn):
    """interested='yes' on a tailored job → changed:False, status stays tailored."""
    jid = _insert(conn, dedupe_key="a|tailored", status="tailored")
    result = apply_decision(conn, jid, interested="yes")
    assert result["changed"] is False
    assert result["status"] == "tailored"
    row = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid),)).fetchone()
    assert row[0] == "tailored"


def test_apply_string_tokens_only(conn):
    """String tokens 'no'/'yes' drive decisions. Sanity check that interested='no' rejects."""
    jid = _insert(conn, dedupe_key="a|strtoken")
    result = apply_decision(conn, jid, interested="no")
    assert result["status"] == "rejected"
    assert result["changed"] is True


def test_apply_unknown_job_id_raises(conn):
    """Random UUID raises ValueError."""
    fake_id = str(uuid.uuid4())
    with pytest.raises(ValueError):
        apply_decision(conn, fake_id, interested="yes")


# ---------------------------------------------------------------------------
# reset_to_routed tests
# ---------------------------------------------------------------------------


def test_reset_from_approved(conn):
    """approved_for_tailoring → reset returns changed:True, status becomes routed."""
    jid = _insert(conn, dedupe_key="r|approved", status="approved_for_tailoring")
    result = reset_to_routed(conn, jid)
    assert result["changed"] is True
    assert str(result["job_id"]) == jid
    row = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid),)).fetchone()
    assert row[0] == "routed"


def test_reset_guard_blocks_tailored(conn):
    """tailored job → reset returns changed:False, status unchanged, no exception raised."""
    jid = _insert(conn, dedupe_key="r|tailored", status="tailored")
    result = reset_to_routed(conn, jid)
    assert result["changed"] is False
    # no exception — benign no-op
    row = conn.execute("select status from jobs where id=%s", (uuid.UUID(jid),)).fetchone()
    assert row[0] == "tailored"


# ---------------------------------------------------------------------------
# fetch_triage_rows — statuses= (IN filter) tests
# ---------------------------------------------------------------------------


def test_fetch_filters_by_statuses_in(conn):
    """statuses=(...) returns only matching statuses; excluded ones are absent."""
    a_new = _insert(conn, dedupe_key="si|new", status="new")
    b_routed = _insert(conn, dedupe_key="si|routed", status="routed")
    c_applied = _insert(conn, dedupe_key="si|applied", status="applied", resume_type="swe")

    rows = fetch_triage_rows(conn, statuses=("new", "routed"))
    ids = {str(r["id"]) for r in rows}

    assert a_new in ids
    assert b_routed in ids
    assert c_applied not in ids
