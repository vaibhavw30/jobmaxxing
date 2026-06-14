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
