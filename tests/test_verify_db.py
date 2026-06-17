from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.normalize import in_window_term_labels
from jobmaxxing.verification.verify import verify_urls

NOW = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _win_term():
    return sorted(in_window_term_labels(NOW.date()))[0]


def _ins(conn, *, dedupe_key, url, alt_urls=None, status="routed", resume_type="swe",
         verify_attempts=0, verified_at=None, source="github:simplify"):
    cols = ["dedupe_key", "source", "company", "title", "url", "resume_type", "status",
            "term", "verify_attempts"]
    vals = [dedupe_key, source, "Acme", "SWE Intern", url, resume_type, status,
            [_win_term()], verify_attempts]
    if alt_urls is not None:
        cols.append("alt_urls"); vals.append(alt_urls)
    if verified_at is not None:
        cols.append("verified_at"); vals.append(verified_at)
    ph = ", ".join(["%s"] * len(vals))
    conn.execute(f"insert into jobs ({', '.join(cols)}) values ({ph})", vals)
    conn.commit()
    return str(conn.execute("select id from jobs where dedupe_key=%s", (dedupe_key,)).fetchone()[0])


def _status_fetcher(status_map):
    """A liveness fetcher: returns the mapped status, or raises (transient) for unknown URLs."""
    def fetch(url):
        if url in status_map:
            return status_map[url]
        raise RuntimeError("unreachable")
    return fetch


def _row(conn, jid):
    return conn.execute(
        "select url, alt_urls, url_status, verify_attempts, verified_at from jobs where id=%s",
        (jid,)).fetchone()


def test_alive_url_marked_alive(conn):
    jid = _ins(conn, dedupe_key="v|alive", url="https://live")
    verify_urls(conn, now=NOW, liveness_fetcher=_status_fetcher({"https://live": 200}),
                find_alt=lambda c, t, u: None)
    url, alts, status, attempts, verified_at = _row(conn, jid)
    assert status == "alive" and url == "https://live" and verified_at is not None


def test_dead_url_promotes_live_alt(conn):
    jid = _ins(conn, dedupe_key="v|alt", url="https://dead", alt_urls=["https://alt"])
    verify_urls(conn, now=NOW,
                liveness_fetcher=_status_fetcher({"https://dead": 404, "https://alt": 200}),
                find_alt=lambda c, t, u: None)
    url, alts, status, _, _ = _row(conn, jid)
    assert status == "alive" and url == "https://alt" and "https://dead" in alts


def test_dead_url_promotes_found_alternative(conn):
    jid = _ins(conn, dedupe_key="v|found", url="https://dead")
    verify_urls(conn, now=NOW,
                liveness_fetcher=_status_fetcher({"https://dead": 404, "https://found": 200}),
                find_alt=lambda c, t, u: "https://found")
    url, alts, status, _, _ = _row(conn, jid)
    assert status == "alive" and url == "https://found" and "https://dead" in alts


def test_found_alternative_that_is_dead_is_not_promoted(conn):
    jid = _ins(conn, dedupe_key="v|founddead", url="https://dead")
    verify_urls(conn, now=NOW,
                liveness_fetcher=_status_fetcher({"https://dead": 404, "https://found": 410}),
                find_alt=lambda c, t, u: "https://found")
    url, alts, status, attempts, _ = _row(conn, jid)
    assert status == "dead" and url == "https://dead" and attempts == 3   # default cap


def test_dead_with_no_alternative_marked_dead_and_capped(conn):
    jid = _ins(conn, dedupe_key="v|dead", url="https://dead")
    verify_urls(conn, now=NOW, cap=3,
                liveness_fetcher=_status_fetcher({"https://dead": 404}),
                find_alt=lambda c, t, u: None)
    _, _, status, attempts, _ = _row(conn, jid)
    assert status == "dead" and attempts == 3


def test_transient_only_increments_attempts(conn):
    jid = _ins(conn, dedupe_key="v|trans", url="https://flaky")
    verify_urls(conn, now=NOW,
                liveness_fetcher=_status_fetcher({"https://flaky": 503}),
                find_alt=lambda c, t, u: None)
    _, _, status, attempts, verified_at = _row(conn, jid)
    assert status is None and attempts == 1 and verified_at is None


def test_candidate_query_excludes_offwindow_decided_capped_and_fresh(conn):
    # off-window (Summer 2016 tag): not a candidate
    conn.execute("insert into jobs (dedupe_key, source, company, title, url, resume_type, status, term, verify_attempts) "
                 "values ('x|off','github:simplify','C','T','https://o','swe','routed',%s,0)",
                 (["Summer 2016"],)); conn.commit()
    # decided
    _ins(conn, dedupe_key="x|dec", url="https://d", status="applied")
    # capped
    _ins(conn, dedupe_key="x|cap", url="https://c", verify_attempts=3)
    # recently verified (within reverify window)
    _ins(conn, dedupe_key="x|fresh", url="https://f", verified_at=NOW - timedelta(days=1))
    counts = verify_urls(conn, now=NOW, cap=3, reverify_days=14,
                         liveness_fetcher=_status_fetcher({}), find_alt=lambda c, t, u: None)
    assert counts["candidates"] == 0


def test_live_alt_wins_over_search(conn):
    # When the primary is dead but a known alt_url resolves, promote the alt and never search.
    jid = _ins(conn, dedupe_key="v|altwins", url="https://dead", alt_urls=["https://alt"])
    searched = []

    def find_alt(c, t, u):
        searched.append(u)
        return "https://found"

    verify_urls(conn, now=NOW, find_alt=find_alt, liveness_fetcher=_status_fetcher(
        {"https://dead": 404, "https://alt": 200, "https://found": 200}))
    url, alts, status, _, _ = _row(conn, jid)
    assert status == "alive" and url == "https://alt"   # alt promoted, not the search result
    assert searched == []                                # search short-circuited by the live alt


def test_verify_urls_holds_no_transaction_during_fetch(conn):
    # The slow liveness-check phase must run with NO open DB transaction held.
    from psycopg.pq import TransactionStatus
    _ins(conn, dedupe_key="v|tx", url="https://live")
    seen = {}

    def recording_fetcher(url):
        seen["status"] = conn.info.transaction_status
        return 200

    verify_urls(conn, now=NOW, liveness_fetcher=recording_fetcher,
                find_alt=lambda c, t, u: None)
    assert seen["status"] == TransactionStatus.IDLE          # not INTRANS during the fetch


def test_fold_alts_orders_old_primary_first_and_excludes_new_primary():
    from jobmaxxing.verification.verify import _fold_alts
    assert _fold_alts("https://new", "https://old",
                      ["https://a", "https://new", "https://a"]) == ["https://old", "https://a"]
