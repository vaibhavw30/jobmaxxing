import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.models import JobRecord
from jobmaxxing.store import upsert_jobs


@pytest.fixture
def conn(postgresql):
    # pytest-postgresql provides a fresh database via the `postgresql` fixture.
    dsn = (
        f"host={postgresql.info.host} port={postgresql.info.port} "
        f"dbname={postgresql.info.dbname} user={postgresql.info.user}"
    )
    with psycopg.connect(dsn) as c:
        yield c


def test_apply_migrations_creates_jobs_table(conn):
    apply_migrations(conn)
    row = conn.execute("select count(*) from jobs").fetchone()
    assert row[0] == 0
    # view exists
    conn.execute("select * from active_unrouted").fetchall()


def test_migration_trims_existing_whitespace_company_and_title(conn):
    apply_migrations(conn)
    # Simulate legacy rows written before the trimming fix (raw insert bypasses
    # JobRecord.__post_init__, which now strips at ingest). Use tab/newline as well
    # as spaces so the backfill matches the ingest path's .strip() (all whitespace).
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url) values "
        "(%s, %s, %s, %s, %s)",
        ("ccc|swe", "github:simplify", "\t CCC Intelligent Solutions", "SWE Intern \n", "https://x"),
    )
    # Re-running migrations applies the idempotent backfill.
    apply_migrations(conn)
    row = conn.execute("select company, title from jobs where dedupe_key = 'ccc|swe'").fetchone()
    assert row == ("CCC Intelligent Solutions", "SWE Intern")
    # Second run is a true no-op: the WHERE guard matches zero rows once clean.
    apply_migrations(conn)
    row = conn.execute("select company, title from jobs where dedupe_key = 'ccc|swe'").fetchone()
    assert row == ("CCC Intelligent Solutions", "SWE Intern")


def _rec(**kw):
    base = dict(source="github:simplify", company="Acme", title="SWE Intern", url="https://list/apply", dedupe_key="acme|swe intern")
    base.update(kw)
    return JobRecord(**base)


def test_upsert_inserts_new_row(conn):
    apply_migrations(conn)
    counts = upsert_jobs(conn, [_rec()])
    assert counts == {"inserted": 1, "merged": 0}
    row = conn.execute("select company, title, url from jobs").fetchone()
    assert row == ("Acme", "SWE Intern", "https://list/apply")


def test_first_seen_at_set_on_insert_and_not_bumped_on_reingest(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec()])
    first = conn.execute(
        "select first_seen_at, scraped_at from jobs where dedupe_key='acme|swe intern'"
    ).fetchone()
    assert first[0] is not None
    # A later poll re-ingests the same posting: the merge UPDATE bumps scraped_at (last-seen) but
    # must leave first_seen_at untouched — that distinction is the whole reason the column exists.
    upsert_jobs(conn, [_rec(description="now enriched")])
    second = conn.execute(
        "select first_seen_at, scraped_at from jobs where dedupe_key='acme|swe intern'"
    ).fetchone()
    assert second[0] == first[0]   # first_seen_at unchanged
    assert second[1] >= first[1]   # scraped_at bumped on re-ingest


def test_upsert_merges_duplicate_and_enriches(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(source="github:simplify", url="https://list/apply", description=None)])
    counts = upsert_jobs(
        conn,
        [_rec(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1", external_id="gh-1", description="full JD")],
    )
    assert counts == {"inserted": 0, "merged": 1}
    row = conn.execute(
        "select count(*), max(url), max(description), max(external_id), max(array_to_string(alt_urls, ',')) from jobs"
    ).fetchone()
    assert row[0] == 1                                   # still one row
    assert row[1] == "https://boards.greenhouse.io/acme/jobs/1"  # ATS url promoted
    assert row[2] == "full JD"                           # description enriched
    assert row[3] == "gh-1"
    assert "https://list/apply" in row[4]                # old url preserved in alt_urls


def test_upsert_is_idempotent(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec()])
    upsert_jobs(conn, [_rec()])
    count = conn.execute("select count(*) from jobs").fetchone()[0]
    assert count == 1


def test_upsert_rejects_empty_dedupe_key(conn):
    apply_migrations(conn)
    with pytest.raises(ValueError):
        upsert_jobs(conn, [_rec(dedupe_key="")])


def test_upsert_validates_batch_before_writing(conn):
    apply_migrations(conn)
    good = _rec(dedupe_key="acme|swe intern")
    bad = _rec(company="Other", title="Role", dedupe_key="")
    with pytest.raises(ValueError):
        upsert_jobs(conn, [good, bad])
    # Validation happens before any write, so nothing was committed.
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 0


def test_upsert_refreshes_scraped_at_on_merge(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec()])
    before = conn.execute("select scraped_at from jobs").fetchone()[0]
    upsert_jobs(conn, [_rec(source="greenhouse", url="https://boards.greenhouse.io/acme/jobs/1")])
    after = conn.execute("select scraped_at from jobs").fetchone()[0]
    assert after >= before   # merge refreshed scraped_at (separate transactions -> now() advances)


def test_upsert_inserts_a_batch_of_new_rows(conn):
    apply_migrations(conn)
    recs = [_rec(dedupe_key=f"acme|swe {i}", title=f"SWE Intern {i}") for i in range(5)]
    counts = upsert_jobs(conn, recs)
    assert counts == {"inserted": 5, "merged": 0}
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 5


def test_upsert_mixed_new_and_existing_batch(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(dedupe_key="acme|swe intern", description=None)])
    counts = upsert_jobs(conn, [
        _rec(dedupe_key="acme|swe intern", source="greenhouse",
             url="https://boards.greenhouse.io/acme/jobs/1", description="full JD"),
        _rec(dedupe_key="acme|ml intern", title="ML Intern"),
    ])
    assert counts == {"inserted": 1, "merged": 1}
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 2
    desc = conn.execute("select description from jobs where dedupe_key='acme|swe intern'").fetchone()[0]
    assert desc == "full JD"


def test_upsert_collapses_intra_batch_duplicate_keys(conn):
    apply_migrations(conn)
    counts = upsert_jobs(conn, [
        _rec(dedupe_key="acme|swe intern", description=None),
        _rec(dedupe_key="acme|swe intern", source="greenhouse",
             url="https://boards.greenhouse.io/acme/jobs/1", description="full JD"),
    ])
    assert counts["inserted"] == 1
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 1
    desc = conn.execute("select description from jobs").fetchone()[0]
    assert desc == "full JD"


def test_upsert_batch_is_idempotent(conn):
    apply_migrations(conn)
    recs = [_rec(dedupe_key=f"acme|swe {i}", title=f"SWE Intern {i}") for i in range(3)]
    upsert_jobs(conn, recs)
    counts = upsert_jobs(conn, recs)
    assert counts == {"inserted": 0, "merged": 3}
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 3


def test_upsert_persists_term(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(term=["Summer 2026", "Fall 2026"])])
    row = conn.execute("select term from jobs where dedupe_key='acme|swe intern'").fetchone()
    assert row[0] == ["Summer 2026", "Fall 2026"]


def test_term_empty_list_and_null_persist_distinctly(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(dedupe_key="a|1", term=[]), _rec(dedupe_key="a|2", term=None)])
    got = dict(conn.execute("select dedupe_key, term from jobs").fetchall())
    assert got["a|1"] == []
    assert got["a|2"] is None


def test_reingest_tags_legacy_null_term_row(conn):
    apply_migrations(conn)
    upsert_jobs(conn, [_rec(term=None)])             # legacy row, no term
    upsert_jobs(conn, [_rec(term=["Summer 2026"])])  # re-seen with a term -> tagged via merge
    row = conn.execute("select term from jobs where dedupe_key='acme|swe intern'").fetchone()
    assert row[0] == ["Summer 2026"]
