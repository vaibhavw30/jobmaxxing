"""Tests for src/jobmaxxing/report.py — the nightly digest builder + emailer."""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.normalize import in_window_term_labels
from jobmaxxing.report import (
    SmtpConfig,
    build_digest,
    load_smtp_config,
    render_html,
    render_text,
    send_digest,
)

NOW = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _ins(conn, *, dedupe_key, term=None, resume_type="swe", status="routed",
         first_seen_at=None, route_confidence=None, posted_at=None,
         source="github:simplify", company="Acme", title="SWE Intern",
         description="jd", recover_attempts=0):
    cols = ["dedupe_key", "source", "company", "title", "url", "description",
            "resume_type", "status", "recover_attempts"]
    vals = [dedupe_key, source, company, title, f"https://x/{dedupe_key}", description,
            resume_type, status, recover_attempts]
    for name, value in (("term", term), ("first_seen_at", first_seen_at),
                        ("route_confidence", route_confidence), ("posted_at", posted_at)):
        if value is not None:
            cols.append(name)
            vals.append(value)
    ph = ", ".join(["%s"] * len(vals))
    conn.execute(f"insert into jobs ({', '.join(cols)}) values ({ph})", vals)
    conn.commit()


def _win():
    return sorted(in_window_term_labels(NOW.date()))


def test_build_digest_counts_new_in_window_undecided(conn):
    win = _win()
    in_term = [win[0]]
    _ins(conn, dedupe_key="r|new", term=in_term, resume_type="swe", status="routed",
         first_seen_at=NOW - timedelta(hours=2), route_confidence=0.9, company="NewCo")
    # in-window but first seen 2 days ago -> in the backlog, NOT "new"
    _ins(conn, dedupe_key="r|old", term=in_term, resume_type="mle", status="routed",
         first_seen_at=NOW - timedelta(days=2))
    # off-window stale tag -> excluded from new AND backlog
    _ins(conn, dedupe_key="r|off", term=["Summer 2016"], status="routed",
         first_seen_at=NOW - timedelta(hours=1))
    # already decided -> not undecided
    _ins(conn, dedupe_key="r|dec", term=in_term, status="applied",
         first_seen_at=NOW - timedelta(hours=1))

    d = build_digest(conn, NOW)
    assert d.window == win
    assert d.new_count == 1
    assert {r["company"] for r in d.new_rows} == {"NewCo"}
    assert d.by_type == {"swe": 1}
    assert d.total_undecided == 2   # new + old; off-window and decided excluded
    assert d.queue_size == 0


def test_build_digest_queue_size_reads_nightly_queue(conn):
    win = _win()
    # JD-less, relevant, exhausted recovery -> appears in the nightly_queue view
    _ins(conn, dedupe_key="q|1", term=[win[0]], resume_type="swe", status="routed",
         first_seen_at=NOW - timedelta(days=5), description="", recover_attempts=2)
    d = build_digest(conn, NOW)
    assert d.queue_size == 1


def test_build_digest_caps_new_rows(conn, monkeypatch):
    import jobmaxxing.report as report
    monkeypatch.setattr(report, "NEW_ROWS_CAP", 3)
    win = _win()
    for i in range(5):
        _ins(conn, dedupe_key=f"c|{i}", term=[win[0]], status="routed",
             first_seen_at=NOW - timedelta(hours=1), route_confidence=0.5)
    d = build_digest(conn, NOW)
    assert d.new_count == 5
    assert len(d.new_rows) == 3   # capped


def test_render_text_includes_key_facts(conn):
    win = _win()
    _ins(conn, dedupe_key="x|1", term=[win[0]], resume_type="swe", status="routed",
         first_seen_at=NOW - timedelta(hours=1), company="ZetaCorp", title="Quant Intern",
         route_confidence=0.95)
    text = render_text(build_digest(conn, NOW))
    assert "1 new in-window role" in text
    assert "ZetaCorp" in text and "Quant Intern" in text
    assert win[0] in text


def test_render_html_escapes_and_links(conn):
    win = _win()
    _ins(conn, dedupe_key="h|1", term=[win[0]], status="routed",
         first_seen_at=NOW - timedelta(hours=1), company="A&B Corp", title="SWE")
    html = render_html(build_digest(conn, NOW))
    assert "<html" in html.lower()
    assert "A&amp;B Corp" in html   # HTML-escaped


def test_load_smtp_config_parses_recipients_and_defaults():
    cfg = load_smtp_config({
        "SMTP_HOST": "smtp.gmail.com", "SMTP_USER": "me@gmail.com",
        "SMTP_PASS": "app-pw", "REPORT_TO": "a@x.com, b@y.com",
    })
    assert cfg.host == "smtp.gmail.com"
    assert cfg.port == 587               # default
    assert cfg.sender == "me@gmail.com"  # defaults to user
    assert cfg.recipients == ["a@x.com", "b@y.com"]


def test_load_smtp_config_errors_on_missing():
    with pytest.raises(RuntimeError):
        load_smtp_config({"SMTP_HOST": "h"})


def test_send_digest_uses_starttls_login_and_recipients(conn):
    d = build_digest(conn, NOW)   # empty digest is fine
    cfg = SmtpConfig(host="h", port=2525, user="u", password="p",
                     sender="u@x.com", recipients=["to1@x.com", "to2@y.com"])
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port):
            calls["addr"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            calls["tls"] = True

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, msg):
            calls["msg"] = msg

    send_digest(d, cfg, smtp_factory=FakeSMTP)
    assert calls["addr"] == ("h", 2525)
    assert calls["tls"] is True
    assert calls["login"] == ("u", "p")
    msg = calls["msg"]
    assert msg["To"] == "to1@x.com, to2@y.com"
    assert msg["From"] == "u@x.com"
    assert msg["Subject"].startswith("jobmaxxing daily")
    assert msg.get_content_type() == "multipart/alternative"  # text + html parts
