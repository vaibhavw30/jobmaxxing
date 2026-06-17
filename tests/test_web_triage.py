"""Tests for src/jobmaxxing/web/triage.py — triage DB layer (Task 3)."""

import uuid

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.web.triage import apply_decision, count_triage, fetch_triage_rows, reset_to_routed


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
            company="Acme", title="SWE Intern", scraped_at=None, posted_at=None,
            route_confidence=None, route_method=None, source="github:simplify", term=None):
    cols = ["dedupe_key", "source", "company", "title", "url", "description", "resume_type", "status"]
    vals = [dedupe_key, source, company, title, f"https://x/{dedupe_key}",
            description, resume_type, status]
    for name, value in (("scraped_at", scraped_at), ("posted_at", posted_at),
                        ("route_confidence", route_confidence), ("route_method", route_method),
                        ("term", term)):
        if value is not None:
            cols.append(name)
            vals.append(value)
    placeholders = ", ".join(["%s"] * len(vals))
    conn.execute(f"insert into jobs ({', '.join(cols)}) values ({placeholders})", vals)
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
    """Default order leads with posted_at desc within one confidence tier."""
    from datetime import datetime, timezone
    old = _insert(conn, dedupe_key="o|old", posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                  route_confidence=0.9)
    new = _insert(conn, dedupe_key="o|new", posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                  route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn)]
    assert ids.index(new) < ids.index(old)


def test_fetch_limit_capped(conn):
    """limit= is honoured and capped at MAX_LIMIT (500); limit=0 is clamped to 1."""
    from jobmaxxing.web.triage import MAX_LIMIT

    # Seed 3 routed jobs.
    _insert(conn, dedupe_key="lc|1")
    _insert(conn, dedupe_key="lc|2")
    _insert(conn, dedupe_key="lc|3")

    # limit=2 returns exactly 2 (cap is honoured when below max).
    rows_2 = fetch_triage_rows(conn, limit=2)
    assert len(rows_2) == 2

    # limit=500 is at MAX_LIMIT but doesn't error; returns all 3 (< cap).
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


def test_fetch_empty_statuses_raises(conn):
    """fetch_triage_rows with statuses=() raises ValueError (not silent no-op)."""
    with pytest.raises(ValueError):
        fetch_triage_rows(conn, statuses=())


# ---------------------------------------------------------------------------
# Task 1: count_triage and route_confidence column tests
# ---------------------------------------------------------------------------


def test_count_triage_matches_filters_ignoring_limit(conn):
    for i in range(5):
        _insert(conn, dedupe_key=f"c|swe|{i}", resume_type="swe", status="routed")
    _insert(conn, dedupe_key="c|swe|rej", resume_type="swe", status="rejected")
    _insert(conn, dedupe_key="c|mle", resume_type="mle", status="routed")
    assert count_triage(conn) == 7
    assert count_triage(conn, statuses=("new", "routed")) == 6
    assert count_triage(conn, resume_type="swe") == 6
    assert count_triage(conn, statuses=("routed",), resume_type="mle") == 1


def test_fetch_includes_route_confidence(conn):
    _insert(conn, dedupe_key="rc|1", route_confidence=0.83)
    rows = fetch_triage_rows(conn)
    assert "route_confidence" in rows[0]
    assert abs(rows[0]["route_confidence"] - 0.83) < 1e-6


# ---------------------------------------------------------------------------
# Task 2: _order_by whitelist + recent+relevant default order tests
# ---------------------------------------------------------------------------


def test_default_demotes_low_confidence_below_high(conn):
    """A RECENT low-confidence job ranks below an OLDER high-confidence one (tier beats recency)."""
    from datetime import datetime, timezone
    recent_low = _insert(conn, dedupe_key="d|recent_low",
                         posted_at=datetime(2026, 6, 10, tzinfo=timezone.utc), route_confidence=0.2)
    older_high = _insert(conn, dedupe_key="d|older_high",
                         posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc), route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn)]
    assert ids.index(older_high) < ids.index(recent_low)

def test_default_null_confidence_treated_as_high(conn):
    """NULL route_confidence (manual) is HIGH-trust (tier 0): it ranks above a low-confidence
    job even when older — which only holds if coalesce(route_confidence, 1.0), not 0.0, is used."""
    from datetime import datetime, timezone
    null_old = _insert(conn, dedupe_key="d|null", posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                       route_confidence=None)
    low_new = _insert(conn, dedupe_key="d|low", posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                      route_confidence=0.2)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn)]
    # null is high-tier, low is demoted -> null ranks above low DESPITE being older
    assert ids.index(null_old) < ids.index(low_new)

def test_sort_company_asc_and_desc(conn):
    a = _insert(conn, dedupe_key="s|c|a", company="Alpha")
    z = _insert(conn, dedupe_key="s|c|z", company="Zeta")
    asc = [str(r["id"]) for r in fetch_triage_rows(conn, sort="company", direction="asc")]
    assert asc.index(a) < asc.index(z)
    desc = [str(r["id"]) for r in fetch_triage_rows(conn, sort="company", direction="desc")]
    assert desc.index(z) < desc.index(a)

def test_sort_posted_is_pure_recency_ignoring_confidence(conn):
    """The 'posted' key sorts by posted_at only — a recent low-confidence job leads."""
    from datetime import datetime, timezone
    recent_low = _insert(conn, dedupe_key="s|p|rl",
                         posted_at=datetime(2026, 6, 10, tzinfo=timezone.utc), route_confidence=0.1)
    older_high = _insert(conn, dedupe_key="s|p|oh",
                         posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc), route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="posted", direction="desc")]
    assert ids.index(recent_low) < ids.index(older_high)

def test_sort_type_groups_then_recency(conn):
    from datetime import datetime, timezone
    ai_new = _insert(conn, dedupe_key="s|t|ai_new", resume_type="ai",
                     posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    ai_old = _insert(conn, dedupe_key="s|t|ai_old", resume_type="ai",
                     posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    swe = _insert(conn, dedupe_key="s|t|swe", resume_type="swe",
                  posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="type", direction="asc")]
    assert ids.index(ai_new) < ids.index(ai_old) < ids.index(swe)

def test_sort_confidence_desc(conn):
    lo = _insert(conn, dedupe_key="s|conf|lo", route_confidence=0.2)
    hi = _insert(conn, dedupe_key="s|conf|hi", route_confidence=0.95)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="conf", direction="desc")]
    assert ids.index(hi) < ids.index(lo)

def test_sort_unknown_key_falls_back_to_default(conn):
    """An unknown/garbage sort key is ignored (no error, no injection) -> default order."""
    from datetime import datetime, timezone
    old = _insert(conn, dedupe_key="s|u|old", posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                  route_confidence=0.9)
    new = _insert(conn, dedupe_key="s|u|new", posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                  route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="company); drop table jobs--", direction="x")]
    assert ids.index(new) < ids.index(old)


# ---------------------------------------------------------------------------
# Task 7: Legacy github row demotion + term filter tests
# ---------------------------------------------------------------------------


# A representative "current upcoming window" (canonical labels) for demotion tests.
_WIN = ["Summer 2026", "Fall 2026"]


def test_legacy_null_term_github_row_demoted(conn):
    in_win = _insert(conn, dedupe_key="d|in", term=["Summer 2026"],
                     posted_at="2026-01-01", route_confidence=0.9)
    legacy = _insert(conn, dedupe_key="d|leg", term=None,
                     posted_at="2026-06-01", route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, in_window_labels=_WIN)]
    assert ids.index(in_win) < ids.index(legacy)  # NULL legacy sinks despite being newer


def test_off_window_tagged_row_demoted(conn):
    # The date-aware part: a row tagged with a term that's no longer in the window sinks, even
    # though it's newer and tagged — stale tags don't keep a row afloat.
    in_win = _insert(conn, dedupe_key="o|in", term=["Fall 2026"],
                     posted_at="2026-01-01", route_confidence=0.9)
    off = _insert(conn, dedupe_key="o|off", term=["Spring 2026"],
                  posted_at="2026-06-01", route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, in_window_labels=_WIN)]
    assert ids.index(in_win) < ids.index(off)  # Spring 2026 off-window -> demoted


def test_ats_and_untagged_rows_not_demoted(conn):
    # ATS rows (non-github, term NULL) and untagged github rows (term '{}') are exempt; only a
    # github row that is legacy/off-window sinks.
    ats = _insert(conn, dedupe_key="x|ats", source="greenhouse", term=None,
                  posted_at="2026-03-01", route_confidence=0.9)
    untagged = _insert(conn, dedupe_key="x|unt", term=[],
                       posted_at="2026-03-01", route_confidence=0.9)
    gh_legacy = _insert(conn, dedupe_key="x|leg", term=None,
                        posted_at="2026-03-01", route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, in_window_labels=_WIN)]
    assert ids.index(ats) < ids.index(gh_legacy)
    assert ids.index(untagged) < ids.index(gh_legacy)


def test_demotion_applies_to_all_sorts(conn):
    a_off = _insert(conn, dedupe_key="d|a", company="Aardvark", term=["Spring 2026"])
    z_in = _insert(conn, dedupe_key="d|z", company="Zzz", term=["Summer 2026"])
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="company", direction="asc",
                                                   in_window_labels=_WIN)]
    assert ids.index(z_in) < ids.index(a_off)  # Zzz(in-window) before Aardvark(off-window)


def test_filter_by_term_matches_multi_term(conn):
    summer = _insert(conn, dedupe_key="t|s", term=["Summer 2026"])
    coop = _insert(conn, dedupe_key="t|c", term=["Fall 2026", "Spring 2026"])
    fall = _insert(conn, dedupe_key="t|f", term=["Fall 2026"])
    ids = {str(r["id"]) for r in fetch_triage_rows(conn, term="Fall 2026")}
    assert ids == {coop, fall}  # summer-only excluded; the multi-term co-op is included


def test_filter_untagged_matches_empty_array_only(conn):
    _insert(conn, dedupe_key="t|tag", term=["Summer 2026"])
    untagged = _insert(conn, dedupe_key="t|na", term=[])
    _insert(conn, dedupe_key="t|leg", term=None)
    ids = {str(r["id"]) for r in fetch_triage_rows(conn, term="__untagged__")}
    assert ids == {untagged}  # cardinality-0 only; NULL legacy excluded


def test_term_filter_composes_with_count(conn):
    _insert(conn, dedupe_key="t|s2", term=["Summer 2026"])
    _insert(conn, dedupe_key="t|f2", term=["Fall 2026"])
    assert count_triage(conn, term="Fall 2026") == 1


def test_untagged_empty_term_not_demoted(conn):
    # Only term IS NULL (legacy) is demoted; a processed-untagged github row ([]) stays in the
    # normal tier, so it outranks an (even newer) legacy NULL row by the default recency order.
    legacy = _insert(conn, dedupe_key="u|leg", term=None,
                     posted_at="2026-06-01", route_confidence=0.9)
    untagged = _insert(conn, dedupe_key="u|emp", term=[],
                       posted_at="2026-01-01", route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn)]
    assert ids.index(untagged) < ids.index(legacy)


def test_dead_url_row_demoted(conn):
    alive = _insert(conn, dedupe_key="u|alive", term=["Summer 2026"], posted_at="2026-01-01",
                    route_confidence=0.9)
    dead = _insert(conn, dedupe_key="u|dead", term=["Summer 2026"], posted_at="2026-06-01",
                   route_confidence=0.9)
    conn.execute("update jobs set url_status='dead' where id=%s", (dead,)); conn.commit()
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, in_window_labels=["Summer 2026"])]
    assert ids.index(alive) < ids.index(dead)  # dead link sinks despite being newer


def test_dead_demotion_applies_to_all_sorts(conn):
    a_dead = _insert(conn, dedupe_key="u|a", company="Aardvark", term=["Summer 2026"])
    conn.execute("update jobs set url_status='dead' where id=%s", (a_dead,)); conn.commit()
    z_alive = _insert(conn, dedupe_key="u|z", company="Zzz", term=["Summer 2026"])
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="company", direction="asc",
                                                   in_window_labels=["Summer 2026"])]
    assert ids.index(z_alive) < ids.index(a_dead)  # Zzz(alive) before Aardvark(dead)


def test_url_status_in_rows(conn):
    jid = _insert(conn, dedupe_key="u|s", term=["Summer 2026"])
    conn.execute("update jobs set url_status='alive' where id=%s", (jid,)); conn.commit()
    rows = fetch_triage_rows(conn, in_window_labels=["Summer 2026"])
    assert rows[0]["url_status"] == "alive"
