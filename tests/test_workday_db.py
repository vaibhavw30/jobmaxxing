import psycopg
import pytest

from jobmaxxing.enrichment.workday import enrich_workday
from jobmaxxing.migrate import apply_migrations


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


_WD = "https://{tenant}.wd5.myworkdayjobs.com/Careers/job/NYC/Intern_R{n}"
_PAYLOAD = {"jobPostingInfo": {"jobDescription": "<p>A real Workday JD with enough words</p>"}}


def _insert(conn, *, dedupe_key, url, description="", attempts=0, route_method=None):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, "
        "enrich_attempts, route_method) values (%s,'github:simplify','Acme','Intern',%s,%s,%s,%s)",
        (dedupe_key, url, description, attempts, route_method),
    )
    conn.commit()


class _OkFetcher:
    """Tier-0 success for every job; records hosts to prove per-tenant handling."""
    def __init__(self):
        self.hosts = []
    def fetch_plain(self, cxs_url):
        self.hosts.append(cxs_url)
        return _PAYLOAD
    def fetch_via_context(self, host, cxs_url):  # pragma: no cover - not reached on Tier-0 success
        return _PAYLOAD
    def fetch_via_render(self, job_url):  # pragma: no cover
        return _PAYLOAD


def test_enrich_workday_fills_descriptions(conn):
    _insert(conn, dedupe_key="w1", url=_WD.format(tenant="acme", n=1))
    counts = enrich_workday(conn, fetcher_factory=_OkFetcher)
    assert counts == {"enriched": 1, "permanent_failed": 0, "transient_failed": 0, "candidates": 1}
    row = conn.execute("select description, enriched_at from jobs where dedupe_key='w1'").fetchone()
    assert row[0] == "<p>A real Workday JD with enough words</p>"
    assert row[1] is not None


def test_enrich_workday_selects_only_eligible_rows(conn):
    _insert(conn, dedupe_key="wd_ok", url=_WD.format(tenant="acme", n=2))
    _insert(conn, dedupe_key="gh", url="https://job-boards.greenhouse.io/acme/jobs/9")   # non-workday
    _insert(conn, dedupe_key="manual", url=_WD.format(tenant="acme", n=3), route_method="manual")
    _insert(conn, dedupe_key="hasdesc", url=_WD.format(tenant="acme", n=4), description="already")
    _insert(conn, dedupe_key="capped", url=_WD.format(tenant="acme", n=5), attempts=3)
    counts = enrich_workday(conn, fetcher_factory=_OkFetcher, cap=3)
    assert counts["candidates"] == 1          # only wd_ok
    assert counts["enriched"] == 1


def test_enrich_workday_transient_then_permanent_classification(conn):
    from jobmaxxing.enrichment.workday import WorkdayBlocked, WorkdayNotFound
    _insert(conn, dedupe_key="blocked", url=_WD.format(tenant="hard", n=6))
    _insert(conn, dedupe_key="gone", url=_WD.format(tenant="dead", n=7))

    class _MixedFetcher:
        def fetch_plain(self, cxs_url):
            if "hard" in cxs_url:
                raise WorkdayBlocked("403")
            raise WorkdayNotFound("404")
        def fetch_via_context(self, host, cxs_url):
            raise WorkdayBlocked("403")
        def fetch_via_render(self, job_url):
            raise WorkdayBlocked("403")

    counts = enrich_workday(conn, fetcher_factory=_MixedFetcher, cap=3)
    assert counts["transient_failed"] == 1    # hard -> blocked all tiers -> transient
    assert counts["permanent_failed"] == 1    # dead -> 404 -> permanent
    assert conn.execute("select enrich_attempts from jobs where dedupe_key='blocked'").fetchone()[0] == 1
    assert conn.execute("select enrich_attempts from jobs where dedupe_key='gone'").fetchone()[0] == 3


def test_enrich_workday_one_fetcher_per_tenant_shard(conn):
    # Two tenants, two jobs each -> 2 fetcher instances (one per tenant shard).
    for n in (10, 11):
        _insert(conn, dedupe_key=f"a{n}", url=_WD.format(tenant="alpha", n=n))
        _insert(conn, dedupe_key=f"b{n}", url=_WD.format(tenant="beta", n=n))
    made = []

    class _CountingFetcher(_OkFetcher):
        def __init__(self):
            super().__init__()
            made.append(self)

    counts = enrich_workday(conn, fetcher_factory=_CountingFetcher, max_workers=2)
    assert counts["enriched"] == 4
    assert len(made) == 2                      # one fetcher per tenant shard, not per job


def test_enrich_workday_respects_max_jobs(conn):
    for n in range(5):
        _insert(conn, dedupe_key=f"m{n}", url=_WD.format(tenant="acme", n=20 + n))
    counts = enrich_workday(conn, fetcher_factory=_OkFetcher, max_jobs=2)
    assert counts["candidates"] == 2


def test_workday_enriched_description_survives_reingest(conn):
    # merge-no-clobber for a Workday row: a re-ingested empty-description row must not wipe
    # the enriched description or the tracking columns (spec §8 invariant).
    from jobmaxxing.models import JobRecord
    from jobmaxxing.store import upsert_jobs
    url = _WD.format(tenant="acme", n=99)
    _insert(conn, dedupe_key="wd|keep", url=url)
    enrich_workday(conn, fetcher_factory=_OkFetcher)
    before = conn.execute("select description, enriched_at from jobs where dedupe_key='wd|keep'").fetchone()
    assert before[0]
    rec = JobRecord(dedupe_key="wd|keep", source="github:simplify", company="Acme",
                    title="Intern", url=url, description=None)
    upsert_jobs(conn, [rec])
    after = conn.execute("select description, enriched_at from jobs where dedupe_key='wd|keep'").fetchone()
    assert after[0] == before[0]      # description preserved
    assert after[1] == before[1]      # enriched_at untouched


def test_cli_shim_exposes_main():
    import jobmaxxing.enrich_workday as cli
    from jobmaxxing.enrichment.workday import main
    assert cli.main is main
