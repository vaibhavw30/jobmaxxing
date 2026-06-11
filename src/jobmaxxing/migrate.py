from pathlib import Path

import psycopg

from .config import REPO_ROOT, load_settings

MIGRATIONS_DIR = REPO_ROOT / "migrations"


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Run every migrations/*.sql in filename order. Idempotent (uses IF NOT EXISTS / OR REPLACE)."""
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
