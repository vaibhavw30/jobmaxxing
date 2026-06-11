from pathlib import Path

import psycopg

from .config import REPO_ROOT, load_settings

MIGRATIONS_DIR = REPO_ROOT / "migrations"


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Run every migrations/*.sql in filename order. Idempotent (uses IF NOT EXISTS / OR REPLACE).

    All files run in one implicit transaction (psycopg3 default), so a failure in any file
    rolls back the whole batch. Do NOT add `CREATE INDEX CONCURRENTLY` (or other DDL that
    cannot run inside a transaction block) to a migration — it will error here.
    """
    if not MIGRATIONS_DIR.is_dir():
        raise RuntimeError(
            f"Migrations directory not found at {MIGRATIONS_DIR}. "
            "Run from the repository checkout (REPO_ROOT is derived from the source layout)."
        )
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.execute(path.read_text())
        applied.append(path.name)
    conn.commit()
    return applied


def main() -> None:
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        applied = apply_migrations(conn)
    print(f"Applied migrations: {', '.join(applied)}")


if __name__ == "__main__":
    main()
