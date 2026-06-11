from datetime import datetime, timezone

import psycopg
import pytest

from jobmaxxing import run as run_module
from jobmaxxing.migrate import apply_migrations
from jobmaxxing.run import build_sources, run_sources


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def test_end_to_end_cross_source_collapse(conn, monkeypatch):
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    list_payload = [
        {
            "company_name": "Acme",
            "title": "Software Engineer Intern",
            "url": "https://simplify.jobs/p/x?utm_source=g",
            "date_posted": int(now.timestamp()),
            "active": True,
            "id": "s1",
        }
    ]
    greenhouse_payload = {
        "jobs": [
            {
                "id": 1001,
                "title": "Software Engineer Intern",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1001",
                "updated_at": "2026-06-01T12:00:00-04:00",
                "location": {"name": "NYC"},
                "content": "Full JD here.",
            }
        ]
    }

    def fake_fetch(url, **kwargs):
        return greenhouse_payload if "greenhouse" in url else list_payload

    monkeypatch.setattr(run_module, "fetch_json", fake_fetch)

    watchlist = [{"company": "Acme", "ats": "greenhouse", "token": "acme"}]
    report = run_sources(conn, build_sources(watchlist=watchlist), now=now)

    # all wired sources ran without error (3 github lists + 1 ATS)
    assert all(r["status"] == "ok" for r in report.values())
    assert report["Acme:greenhouse:acme"]["status"] == "ok"

    # the same role from 4 sources collapses to exactly one row
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 1
    url, description, alt = conn.execute(
        "select url, description, array_to_string(alt_urls, ',') from jobs"
    ).fetchone()
    assert url == "https://boards.greenhouse.io/acme/jobs/1001"   # ATS url promoted
    assert description == "Full JD here."                          # enriched from ATS JD
    assert "https://simplify.jobs/p/x" in alt                     # list link preserved (canonicalized)
