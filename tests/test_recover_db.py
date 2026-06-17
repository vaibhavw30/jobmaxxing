import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def test_migration_adds_recovery_columns(conn):
    cols = {r[0] for r in conn.execute(
        "select column_name from information_schema.columns where table_name='jobs'"
    ).fetchall()}
    assert {"jd_source", "recover_attempts", "recover_error"} <= cols


from jobmaxxing.recovery.recover import recover_new

_WD = "https://acme.wd1.myworkdayjobs.com/Ext/job/NYC/ML-Intern_JR{n}"
_CAND_HTML = (
    '<script type="application/ld+json">'
    '{{"@type":"JobPosting","title":"ML Intern","description":"<p>Recovered JD {n}</p>",'
    '"hiringOrganization":"Acme","identifier":"JR{n}"}}</script>'
)


def _insert(conn, *, dedupe_key, url, description="", resume_type="mle", route_method="llm_title",
            recover_attempts=0, company="Acme", title="ML Intern"):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, "
        "route_method, recover_attempts) values (%s,'github:simplify',%s,%s,%s,%s,%s,%s,%s)",
        (dedupe_key, company, title, url, description, resume_type, route_method, recover_attempts),
    )
    conn.commit()


def _searcher_ok(query, *, fetch_text):      # one candidate result URL
    return ["https://glassdoor.com/job/1"]


def _llm_never(job, cand):
    raise AssertionError("llm_confirm should not be needed (req-id matches)")


def test_recover_writes_jd_and_resets_routing(conn):
    _insert(conn, dedupe_key="r1", url=_WD.format(n="012226"))

    def fetcher(url):
        return _CAND_HTML.format(n="012226")   # carries identifier JR012226 == the job's req-id

    counts = recover_new(conn, searcher=_searcher_ok, fetcher=fetcher, llm_confirm=_llm_never)
    assert counts == {"recovered": 1, "missed": 0, "candidates": 1}
    row = conn.execute(
        "select description, jd_source, resume_type, route_method from jobs where dedupe_key='r1'"
    ).fetchone()
    assert row[0] == "<p>Recovered JD 012226</p>" and row[1] == "recovered"
    assert row[2] is None and row[3] is None    # reset so CI re-routes with the JD


def test_recover_miss_bumps_attempts_and_caps(conn):
    _insert(conn, dedupe_key="r2", url=_WD.format(n="999"))

    def fetcher(url):
        return _CAND_HTML.format(n="DIFFERENT")   # identifier mismatch, company 'Acme' but...

    # company matches (Acme) + title similar (ML Intern) -> falls to llm_confirm; force a reject
    counts = recover_new(conn, searcher=_searcher_ok, fetcher=fetcher, llm_confirm=lambda j, c: False, cap=2)
    assert counts["missed"] == 1
    assert conn.execute("select recover_attempts from jobs where dedupe_key='r2'").fetchone()[0] == 1
    # bump to the cap -> no longer selected
    recover_new(conn, searcher=_searcher_ok, fetcher=fetcher, llm_confirm=lambda j, c: False, cap=2)
    assert recover_new(conn, searcher=_searcher_ok, fetcher=fetcher, llm_confirm=lambda j, c: False, cap=2)["candidates"] == 0


def test_recover_selects_only_relevant_jdless_workday(conn):
    _insert(conn, dedupe_key="ok", url=_WD.format(n="1"))                                   # selected
    _insert(conn, dedupe_key="hasdesc", url=_WD.format(n="2"), description="already")        # has JD
    _insert(conn, dedupe_key="norel", url=_WD.format(n="3"), resume_type=None, route_method=None)  # not relevant
    _insert(conn, dedupe_key="manual", url=_WD.format(n="4"), route_method="manual")         # manual
    _insert(conn, dedupe_key="gh", url="https://job-boards.greenhouse.io/acme/jobs/9")       # non-workday

    def fetcher(url):
        return _CAND_HTML.format(n="1")

    counts = recover_new(conn, searcher=_searcher_ok, fetcher=fetcher, llm_confirm=_llm_never)
    assert counts["candidates"] == 1 and counts["recovered"] == 1


def test_recover_search_exception_is_missed_with_error(conn):
    _insert(conn, dedupe_key="serr", url=_WD.format(n="5"))

    def boom_searcher(query, *, fetch_text):
        raise RuntimeError("ddg down")

    counts = recover_new(conn, searcher=boom_searcher, fetcher=lambda u: "", llm_confirm=lambda j, c: False)
    assert counts == {"recovered": 0, "missed": 1, "candidates": 1}
    row = conn.execute(
        "select recover_attempts, recover_error from jobs where dedupe_key='serr'"
    ).fetchone()
    assert row[0] == 1 and row[1] is not None          # attempt bumped + a diagnostic error recorded


def test_recover_new_holds_no_transaction_during_fetch(conn):
    # The slow search/fetch phase must run with NO open DB transaction held.
    from psycopg.pq import TransactionStatus
    _insert(conn, dedupe_key="tx", url=_WD.format(n="1"))
    seen = {}

    def recording_searcher(query, *, fetch_text):
        seen["status"] = conn.info.transaction_status
        return []                                            # no candidates -> a clean miss

    recover_new(conn, searcher=recording_searcher, fetcher=lambda u: "",
                llm_confirm=lambda j, c: False)
    assert seen["status"] == TransactionStatus.IDLE          # not INTRANS during the fetch


def test_recover_cli_shim_exposes_main():
    import jobmaxxing.recover_jd as cli
    from jobmaxxing.recovery.recover import main
    assert cli.main is main
