"""Guards against schema drift between the app's runtime DDL and the SQL migrations.

Every table the application can create at runtime (local SQLite dev fallback) must
also be defined by a versioned migration, so a hosted PostgreSQL database (where the
app no longer creates tables as a side effect) has the complete schema.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z0-9_]+)", re.IGNORECASE)


def _tables_in(text: str) -> set:
    return {m.group(1).lower() for m in _CREATE.finditer(text)}


def test_every_runtime_table_is_covered_by_a_migration():
    runtime = set()
    for rel in ("app/business_documents.py", "app/website_intelligence.py"):
        runtime |= _tables_in((ROOT / rel).read_text())

    migrated = set()
    for sql in sorted((ROOT / "database" / "migrations").glob("*.sql")):
        migrated |= _tables_in(sql.read_text())

    missing = runtime - migrated
    assert not missing, f"tables created at runtime but absent from migrations: {sorted(missing)}"


def test_business_documents_does_not_create_tables_on_postgres_path(monkeypatch, tmp_path):
    import app.business_documents as bd

    class _FakeCursor:
        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class _FakePgConn:
        def execute(self, sql, params=()):
            assert "create table" not in sql.lower(), f"hosted path issued DDL: {sql[:60]}"
            return _FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    class _FakePsycopg:
        @staticmethod
        def connect(dsn, connect_timeout=5):
            return _FakePgConn()

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost/db")
    monkeypatch.setitem(__import__("sys").modules, "psycopg", _FakePsycopg)
    conn = bd._db()
    assert bd.is_postgres(conn) is True


def test_business_documents_uses_sqlite_and_auto_creates_locally(monkeypatch, tmp_path):
    import app.business_documents as bd

    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("MIA_BUSINESS_DATA_PATH", str(tmp_path / "b.sqlite3"))
    conn = bd._db()
    try:
        assert bd.is_postgres(conn) is False
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"business_profiles", "business_files"}.issubset(names)
    finally:
        conn.close()
