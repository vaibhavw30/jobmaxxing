import httpx
import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations


@pytest.fixture
def conn(postgresql):
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def test_migration_adds_enrichment_columns(conn):
    cols = {
        row[0]
        for row in conn.execute(
            "select column_name from information_schema.columns where table_name='jobs'"
        ).fetchall()
    }
    assert {"enrich_attempts", "enriched_at", "enrich_error"} <= cols


# ---------------------------------------------------------------------------
# Task 6 — _fetch_one (pure, no DB, no network). Imports deferred so Task 1
# stays collectable while the sibling adapters module is not yet merged.
# ---------------------------------------------------------------------------

def _http_error(status):
    req = httpx.Request("GET", "https://api.example/x")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


def test_fetch_one_enriched_on_success():
    from jobmaxxing.enrichment.enrich import _fetch_one
    def fake(api_url):
        return {"content": "&lt;p&gt;hi&lt;/p&gt;"}
    out = _fetch_one("id1", "https://job-boards.greenhouse.io/x/jobs/1", fake)
    assert out.kind == "enriched"
    assert out.description == "<p>hi</p>"


def test_fetch_one_permanent_on_404():
    from jobmaxxing.enrichment.enrich import _fetch_one
    def fake(api_url):
        raise _http_error(404)
    out = _fetch_one("id1", "https://job-boards.greenhouse.io/x/jobs/1", fake)
    assert out.kind == "permanent"


def test_fetch_one_transient_on_429_and_timeout():
    from jobmaxxing.enrichment.enrich import _fetch_one
    def fake_429(api_url):
        raise _http_error(429)
    def fake_timeout(api_url):
        raise httpx.TimeoutException("slow")
    assert _fetch_one("i", "https://job-boards.greenhouse.io/x/jobs/1", fake_429).kind == "transient"
    assert _fetch_one("i", "https://job-boards.greenhouse.io/x/jobs/1", fake_timeout).kind == "transient"


def test_fetch_one_permanent_when_no_description_parsed():
    from jobmaxxing.enrichment.enrich import _fetch_one
    def fake(api_url):
        return {"content": ""}
    out = _fetch_one("id1", "https://job-boards.greenhouse.io/x/jobs/1", fake)
    assert out.kind == "permanent"


def test_fetch_one_permanent_when_unsupported_host():
    from jobmaxxing.enrichment.enrich import _fetch_one
    def fake(api_url):
        raise AssertionError("must not fetch an unsupported host")
    out = _fetch_one("id1", "https://comcast.wd5.myworkdayjobs.com/x/job/y/z_R1", fake)
    assert out.kind == "permanent"


# ---------------------------------------------------------------------------
# Task 7 — enrich_new (DB tests with fake fetcher)
# ---------------------------------------------------------------------------

_GH = "https://job-boards.greenhouse.io/acme/jobs/{n}"


def _insert(conn, *, dedupe_key, url, description="", attempts=0, route_method=None):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, "
        "enrich_attempts, route_method) values (%s,'github:simplify','Acme','Intern',%s,%s,%s,%s)",
        (dedupe_key, url, description, attempts, route_method),
    )
    conn.commit()


def _fake_fetch_ok(api_url):
    return {"content": "&lt;p&gt;A real JD with enough words&lt;/p&gt;"}


def test_enrich_new_fills_description_and_sets_enriched_at(conn):
    from jobmaxxing.enrichment.enrich import enrich_new
    _insert(conn, dedupe_key="a", url=_GH.format(n=1))
    counts = enrich_new(conn, fetch_json=_fake_fetch_ok)
    assert counts == {"enriched": 1, "permanent_failed": 0, "transient_failed": 0, "candidates": 1}
    row = conn.execute(
        "select description, enriched_at, enrich_attempts from jobs where dedupe_key='a'"
    ).fetchone()
    assert row[0] == "<p>A real JD with enough words</p>"
    assert row[1] is not None
    assert row[2] == 0


def test_enrich_new_marks_permanent_and_stops_reselecting(conn):
    from jobmaxxing.enrichment.enrich import enrich_new
    _insert(conn, dedupe_key="b", url=_GH.format(n=2))

    def fake_404(api_url):
        req = httpx.Request("GET", api_url)
        raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))

    counts = enrich_new(conn, fetch_json=fake_404, cap=3)
    assert counts["permanent_failed"] == 1
    assert conn.execute("select enrich_attempts from jobs where dedupe_key='b'").fetchone()[0] == 3
    # second run: not reselected (attempts >= cap)
    counts2 = enrich_new(conn, fetch_json=fake_404, cap=3)
    assert counts2["candidates"] == 0


def test_enrich_new_transient_increments_then_caps(conn):
    from jobmaxxing.enrichment.enrich import enrich_new
    _insert(conn, dedupe_key="c", url=_GH.format(n=3))

    def fake_timeout(api_url):
        raise httpx.TimeoutException("slow")

    for expected in (1, 2, 3):
        enrich_new(conn, fetch_json=fake_timeout, cap=3)
        got = conn.execute("select enrich_attempts from jobs where dedupe_key='c'").fetchone()[0]
        assert got == expected
    # now attempts == cap -> no longer a candidate
    assert enrich_new(conn, fetch_json=fake_timeout, cap=3)["candidates"] == 0


def test_enrich_new_respects_max_fetches(conn):
    from jobmaxxing.enrichment.enrich import enrich_new
    for i in range(5):
        _insert(conn, dedupe_key=f"d{i}", url=_GH.format(n=100 + i))
    counts = enrich_new(conn, fetch_json=_fake_fetch_ok, max_fetches=2)
    assert counts["candidates"] == 2
    assert counts["enriched"] == 2


def test_enrich_new_skips_unsupported_and_manual_and_already_described(conn):
    from jobmaxxing.enrichment.enrich import enrich_new
    _insert(conn, dedupe_key="wd", url="https://x.wd5.myworkdayjobs.com/en/x/job/y/z_R1")  # unsupported host
    _insert(conn, dedupe_key="man", url=_GH.format(n=9), route_method="manual")            # manual
    _insert(conn, dedupe_key="has", url=_GH.format(n=10), description="already here")        # has desc

    def fake_boom(api_url):
        raise AssertionError("should not fetch any of these rows")

    assert enrich_new(conn, fetch_json=fake_boom)["candidates"] == 0


# ---------------------------------------------------------------------------
# Task 8 — merge-no-clobber durability
# ---------------------------------------------------------------------------

def test_enriched_description_survives_reingest(conn):
    from jobmaxxing.enrichment.enrich import enrich_new
    from jobmaxxing.models import JobRecord
    from jobmaxxing.store import upsert_jobs
    _insert(conn, dedupe_key="keep|me", url=_GH.format(n=42))
    enrich_new(conn, fetch_json=_fake_fetch_ok)
    before = conn.execute(
        "select description, enriched_at from jobs where dedupe_key='keep|me'"
    ).fetchone()
    assert before[0] == "<p>A real JD with enough words</p>"

    # GitHub list re-ingests the same job with NO description.
    rec = JobRecord(
        dedupe_key="keep|me", source="github:simplify", company="Acme", title="Intern",
        url=_GH.format(n=42), description=None,
    )
    upsert_jobs(conn, [rec])

    after = conn.execute(
        "select description, enriched_at from jobs where dedupe_key='keep|me'"
    ).fetchone()
    assert after[0] == before[0]      # description preserved (empty is falsy in merge)
    assert after[1] == before[1]      # enriched_at untouched by _UPDATE_SQL
