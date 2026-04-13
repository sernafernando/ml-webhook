import os


os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/testdb")
os.environ.setdefault("DEBUG_WEBHOOK", "0")


try:
    from psycopg2 import pool as psycopg2_pool

    class _DummyPool:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def getconn(self):  # pragma: no cover - tests reemplazan db_cursor
            raise RuntimeError("Dummy pool: tests must monkeypatch db_cursor")

        def putconn(self, conn):  # pragma: no cover
            return None

    psycopg2_pool.SimpleConnectionPool = _DummyPool
except Exception:
    pass
