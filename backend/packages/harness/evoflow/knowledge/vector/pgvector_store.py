"""pgvector-backed vector store for the evoflow knowledge base (PostgreSQL mode).

Same surface as :class:`evoflow.knowledge.vector.sqlite_vec.VectorStore`;
selected automatically when ``EVOFLOW_DB_URL`` is set. Storage model:

- Per-dataset ordinary table ``evoflow_kb_vectors_<dataset_id>`` with
  ``chunk_id TEXT PRIMARY KEY, embedding vector(N)`` (mirrors the per-dataset
  ``vec0`` tables of the SQLite backend, which exist for the same reason:
  the vector column width is fixed per table).
- Recall uses pgvector's cosine-distance operator ``<=>`` — the same metric
  as ``vec0`` ``MATCH`` (``distance = 1 - cosine_similarity``), so
  ``score = 1 - distance`` stays consistent across backends.
- Embeddings are passed as ``'[0.1, 0.2, ...]'`` text cast to ``::vector`` —
  no extra Python driver package needed.

Requires the ``vector`` extension: ``init()`` runs
``CREATE EXTENSION IF NOT EXISTS vector`` (needs a preinstalled extension +
appropriate privileges). In compose, use the ``pgvector/pgvector:pg16``
image. When the extension is unavailable, init raises :class:`VectorStoreError`
with an actionable message; the rest of the app is unaffected.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from evoflow.knowledge.vector.base import (
    DEFAULT_EMBEDDING_DIM,
    RecallHit,
    VectorStoreError,
    vectors_table_for,
)
from evoflow.persistence.db import db_connection_lock, get_db

logger = logging.getLogger(__name__)


def _to_pgvector(values: Sequence[float]) -> str:
    """Render a float sequence as a pgvector literal (``'[0.1,0.2,...]'``)."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


class PgVectorStore:
    """Cosine-similarity vector store over pgvector (PostgreSQL backend)."""

    def __init__(self, dataset_id: str, dim: int | None = None) -> None:
        if not dataset_id:
            raise VectorStoreError("dataset_id is required")
        self.dataset_id = dataset_id
        self._dim: int | None = dim
        if dim is not None and dim <= 0:
            raise VectorStoreError(f"dim must be positive, got {dim}")

    @property
    def dim(self) -> int:
        if self._dim is None:
            raise VectorStoreError("VectorStore not initialized; call init() first")
        return self._dim

    # ------------------------------------------------------------------ init
    def init(self, *, name: str | None = None, embedding_model: str = "") -> int:
        """Ensure the dataset row + pgvector table exist. Returns the dim."""
        with db_connection_lock():
            conn = get_db()
            dim = self._resolve_or_create_dataset(conn, name=name, embedding_model=embedding_model)
            self._dim = dim
            self._ensure_extension(conn)
            self._ensure_vectors_table(conn, dim)
        return dim

    def _resolve_or_create_dataset(self, conn: Any, *, name: str | None, embedding_model: str) -> int:
        row = conn.execute(
            "SELECT embedding_dim FROM evoflow_kb_dataset WHERE dataset_id = ?",
            (self.dataset_id,),
        ).fetchone()
        if row is not None:
            return int(row["embedding_dim"])
        dim = self._dim or DEFAULT_EMBEDDING_DIM
        conn.execute(
            """
            INSERT INTO evoflow_kb_dataset (dataset_id, name, embedding_model, embedding_dim)
            VALUES (?, ?, ?, ?)
            """,
            (self.dataset_id, name or self.dataset_id, embedding_model, dim),
        )
        conn.commit()
        return dim

    def _ensure_extension(self, conn: Any) -> None:
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - privileges / not installed
            raise VectorStoreError(
                "pgvector extension is not available on this PostgreSQL server. "
                "Use the pgvector/pgvector:pg16 image (compose cloud profile) or "
                "have the DBA run: CREATE EXTENSION vector;"
            ) from exc

    def _ensure_vectors_table(self, conn: Any, dim: int) -> None:
        table = vectors_table_for(self.dataset_id)
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "chunk_id TEXT PRIMARY KEY, "
            f"embedding vector({int(dim)}))"
        )
        # Index is optional at this scale: hnsw build needs the table populated
        # to be effective, and small datasets scan fine without it. Created
        # best-effort; recall works regardless.
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {table}_hnsw ON {table} "
                "USING hnsw (embedding vector_cosine_ops)"
            )
        except Exception:  # noqa: BLE001
            logger.debug("pgvector: hnsw index not created for %s", table, exc_info=True)
        conn.commit()

    # ---------------------------------------------------------------- helpers
    def _check_dim(self, embedding: Sequence[float]) -> None:
        dim = self.dim
        if len(embedding) != dim:
            raise VectorStoreError(
                f"embedding dimension mismatch: expected {dim}, got {len(embedding)}"
            )

    @staticmethod
    def _table(dataset_id: str) -> str:
        return vectors_table_for(dataset_id)

    # ---------------------------------------------------------------- insert
    def insert(self, chunk_id: str, embedding: Sequence[float]) -> None:
        self._check_dim(embedding)
        with db_connection_lock():
            conn = get_db()
            conn.execute(
                f"INSERT INTO {self._table(self.dataset_id)} (chunk_id, embedding) "
                "VALUES (?, ?::vector) "
                "ON CONFLICT(chunk_id) DO UPDATE SET embedding = excluded.embedding",
                (chunk_id, _to_pgvector(embedding)),
            )
            conn.commit()

    def insert_many(self, items: Iterable[tuple[str, Sequence[float]]]) -> int:
        dim = self.dim
        rows: list[tuple[str, str]] = []
        for chunk_id, embedding in items:
            if len(embedding) != dim:
                raise VectorStoreError(
                    f"embedding dimension mismatch for chunk {chunk_id}: expected {dim}, got {len(embedding)}"
                )
            rows.append((chunk_id, _to_pgvector(embedding)))
        if not rows:
            return 0
        with db_connection_lock():
            conn = get_db()
            conn.executemany(
                f"INSERT INTO {self._table(self.dataset_id)} (chunk_id, embedding) "
                "VALUES (?, ?::vector) "
                "ON CONFLICT(chunk_id) DO UPDATE SET embedding = excluded.embedding",
                rows,
            )
            conn.commit()
        return len(rows)

    # ---------------------------------------------------------------- delete
    def delete(self, chunk_id: str) -> None:
        with db_connection_lock():
            conn = get_db()
            conn.execute(
                f"DELETE FROM {self._table(self.dataset_id)} WHERE chunk_id = ?",
                (chunk_id,),
            )
            conn.commit()

    def delete_many(self, chunk_ids: Iterable[str]) -> int:
        ids = [cid for cid in chunk_ids]
        if not ids:
            return 0
        with db_connection_lock():
            conn = get_db()
            cur = conn.executemany(
                f"DELETE FROM {self._table(self.dataset_id)} WHERE chunk_id = ?",
                [(cid,) for cid in ids],
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def delete_by_dataset(self) -> int:
        with db_connection_lock():
            conn = get_db()
            ids = [
                r["chunk_id"]
                for r in conn.execute(
                    "SELECT chunk_id FROM evoflow_kb_chunk WHERE dataset_id = ?",
                    (self.dataset_id,),
                ).fetchall()
            ]
        if not ids:
            return 0
        return self.delete_many(ids)

    # ----------------------------------------------------------------- recall
    def recall(self, query_embedding: Sequence[float], *, top_k: int = 5) -> list[RecallHit]:
        """Return the ``top_k`` nearest chunks by cosine similarity.

        pgvector's ``<=>`` is cosine *distance* — identical semantics to the
        SQLite backend's ``vec0`` ``MATCH`` distance (``1 - similarity``).
        """
        self._check_dim(query_embedding)
        k = max(1, int(top_k))
        q = _to_pgvector(query_embedding)
        table = self._table(self.dataset_id)
        with db_connection_lock():
            conn = get_db()
            rows = conn.execute(
                f"SELECT chunk_id, embedding <=> ?::vector AS distance FROM {table} "
                "ORDER BY embedding <=> ?::vector LIMIT ?",
                (q, q, k),
            ).fetchall()
        return [
            RecallHit(chunk_id=str(r["chunk_id"]), distance=float(r["distance"]), score=1.0 - float(r["distance"]))
            for r in rows
        ]

    # -------------------------------------------------------------- get_count
    def get_count(self) -> int:
        with db_connection_lock():
            conn = get_db()
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM {self._table(self.dataset_id)}"
            ).fetchone()
        return int(row["c"]) if row is not None else 0

    def get_dataset_count(self) -> int:
        with db_connection_lock():
            conn = get_db()
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM evoflow_kb_chunk WHERE dataset_id = ?",
                (self.dataset_id,),
            ).fetchone()
        return int(row["c"]) if row is not None else 0
