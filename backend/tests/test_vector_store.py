"""Vector store tests: facade dispatch + SQLite roundtrip + pgvector SQL.

Run: cd backend && uv run pytest tests/test_vector_store.py -v

The pgvector live path (real extension, real recall) runs in CI via
EVOFLOW_DB_URL (see tests/test_pg_backend.py::TestLivePG).
"""

from __future__ import annotations

import pytest

from evoflow.knowledge.vector import VectorStore, VectorStoreError
from evoflow.knowledge.vector.sqlite_vec import _SqliteVecStore


# ---------------------------------------------------------------------------
# Facade dispatch
# ---------------------------------------------------------------------------


def test_facade_defaults_to_sqlite():
    vs = VectorStore("ds")
    assert isinstance(vs._impl, _SqliteVecStore)


def test_facade_dispatches_to_pgvector(monkeypatch):
    monkeypatch.setenv("EVOFLOW_DB_URL", "postgresql://u:p@localhost:5432/db")
    from evoflow.knowledge.vector.pgvector_store import PgVectorStore

    vs = VectorStore("ds")
    assert isinstance(vs._impl, PgVectorStore)


def test_facade_rejects_bad_dataset():
    with pytest.raises(VectorStoreError):
        VectorStore("")
    with pytest.raises(VectorStoreError):
        VectorStore("ds", dim=0)


# ---------------------------------------------------------------------------
# SQLite backend roundtrip (real sqlite-vec extension, temp DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_sqlite_db(tmp_path, monkeypatch):
    monkeypatch.delenv("EVOFLOW_DB_URL", raising=False)
    monkeypatch.setenv("EVOFLOW_DB_PATH", str(tmp_path / "evoflow.db"))
    from evoflow.persistence import db as db_mod

    db_mod.reset_db_for_tests()
    yield db_mod
    db_mod.reset_db_for_tests()


def test_sqlite_roundtrip(fresh_sqlite_db):
    vs = VectorStore("ci_ds", dim=4)
    assert vs.init(name="CI", embedding_model="test-embed") == 4
    # init is idempotent and resolves dim from the dataset row
    assert VectorStore("ci_ds").init() == 4

    vs.insert("c1", [1.0, 0.0, 0.0, 0.0])
    assert vs.insert_many(
        [("c2", [0.0, 1.0, 0.0, 0.0]), ("c3", [0.0, 0.0, 1.0, 0.0])]
    ) == 2
    assert vs.get_count() == 3

    hits = vs.recall([1.0, 0.0, 0.0, 0.0], top_k=2)
    assert len(hits) == 2
    assert hits[0].chunk_id == "c1"
    assert hits[0].score > 0.99  # exact direction match
    assert hits[0].distance == pytest.approx(0.0, abs=1e-4)

    # dim mismatch raises on both insert and recall
    with pytest.raises(VectorStoreError):
        vs.insert("c4", [1.0, 2.0])
    with pytest.raises(VectorStoreError):
        vs.recall([1.0, 2.0])

    assert vs.delete_many(["c2", "c3"]) == 2
    assert vs.get_count() == 1
    vs.delete("c1")
    assert vs.get_count() == 0


# ---------------------------------------------------------------------------
# pgvector backend: SQL generation through the real translation pipeline
# ---------------------------------------------------------------------------


class _FakeInfo:
    transaction_status = 0


class _RecCursor:
    def __init__(self, conn: "_RecConn") -> None:
        self._conn = conn
        self.description = None
        self.rowcount = -1
        self._rows: list = []

    def execute(self, sql, params=()):
        self._conn.log.append(("exec", sql, params))
        self.description, self._rows = self._conn.respond(sql, params)

    def executemany(self, sql, rows):
        self._conn.log.append(("many", sql, list(rows)))
        self.description, self._rows = None, []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _RecConn:
    """Records statements; answers the dataset-dimension probe."""

    def __init__(self) -> None:
        self.log: list = []
        self.info = _FakeInfo()

    def cursor(self):
        return _RecCursor(self)

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))

    def close(self):
        pass

    def respond(self, sql, params):
        s = sql.strip()
        if s.startswith("SELECT embedding_dim"):
            # dataset row missing -> create path (returns no rows)
            return [("embedding_dim",)], []
        if "SELECT lastval()" in s:
            return [("lastval",)], [(1,)]
        if s.upper().startswith("SELECT COUNT") and "evoflow_kb_vectors" in s:
            return [("c",)], [(2,)]
        if "<=>" in s:
            # recall: two vectors, distances by order of insert
            return [("chunk_id",), ("distance",)], [("c1", 0.1), ("c2", 0.4)]
        return None, []


@pytest.fixture
def pg_vector_store(monkeypatch):
    """PgVectorStore bound to a recording fake through the real wrapper."""
    monkeypatch.setenv("EVOFLOW_DB_URL", "postgresql://u:p@localhost:5432/db")
    from evoflow.persistence import db_backend
    from evoflow.persistence.db import reset_db_for_tests
    from evoflow.knowledge.vector import pgvector_store as pvs

    fake = _RecConn()
    wrapper = db_backend.PostgresConnection(fake)
    monkeypatch.setattr(pvs, "get_db", lambda *a, **k: wrapper)
    monkeypatch.setattr(pvs, "db_connection_lock", lambda: wrapper.connection_lock)
    reset_db_for_tests()
    yield pvs.PgVectorStore("ci_ds", dim=4)
    reset_db_for_tests()


def test_pgvector_init_sql(pg_vector_store):
    dim = pg_vector_store.init(name="CI", embedding_model="m")
    assert dim == 4
    fake = _underlying_fake(pg_vector_store)
    sqls = [e[1] for e in fake.log if e[0] == "exec"]
    assert any(s.startswith("CREATE EXTENSION IF NOT EXISTS vector") for s in sqls)
    assert any(
        s.startswith("CREATE TABLE IF NOT EXISTS evoflow_kb_vectors_ci_ds")
        and "embedding vector(4)" in s
        for s in sqls
    )
    assert any("USING hnsw" in s and "vector_cosine_ops" in s for s in sqls)
    # the dataset row was created with the requested dim (multi-line SQL
    # statement — match on stripped text)
    assert any(
        "INSERT INTO evoflow_kb_dataset" in s for s in sqls
    )


def test_pgvector_insert_sql(pg_vector_store):
    fake = _underlying_fake(pg_vector_store)
    fake.log.clear()
    pg_vector_store.insert("c1", [1.0, 0.0, 0.0, 0.0])
    kind, sql, params = fake.log[0]
    assert sql == (
        "INSERT INTO evoflow_kb_vectors_ci_ds (chunk_id, embedding) "
        "VALUES (%s, %s::vector) "
        "ON CONFLICT(chunk_id) DO UPDATE SET embedding = excluded.embedding"
    )
    assert params[0] == "c1"
    assert params[1].startswith("[1.0,0.0,0.0,0.0]")


def test_pgvector_recall_sql(pg_vector_store):
    fake = _underlying_fake(pg_vector_store)
    fake.log.clear()
    hits = pg_vector_store.recall([1.0, 0.0, 0.0, 0.0], top_k=5)
    assert [h.chunk_id for h in hits] == ["c1", "c2"]
    assert hits[0].score == pytest.approx(0.9, abs=1e-6)  # 1 - 0.1
    kind, sql, params = fake.log[0]
    assert sql == (
        "SELECT chunk_id, embedding <=> %s::vector AS distance FROM evoflow_kb_vectors_ci_ds "
        "ORDER BY embedding <=> %s::vector LIMIT %s"
    )
    assert params[2] == 5


def _underlying_fake(store):
    # the fixture monkeypatched pvs.get_db to a wrapper over _RecConn; the
    # wrapper stored the fake — reach through the patched module's closure
    from evoflow.knowledge.vector import pgvector_store as pvs

    return pvs.get_db()._conn
