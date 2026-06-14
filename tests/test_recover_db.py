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
