"""Vector store sub-package for the evoflow knowledge base.

Two backends behind one surface (:class:`VectorStore`):

- **sqlite-vec** (default): ``vec0`` virtual table on the shared SQLite
  connection; embeddings are packed float32 blobs, recall uses ``vec0``'s
  native cosine distance.
- **pgvector** (PostgreSQL mode, ``EVOFLOW_DB_URL`` set): per-dataset
  ``vector(N)`` tables with cosine recall via ``<=>`` — same distance
  semantics (``distance = 1 - similarity``) as the SQLite backend.

Vector dimensionality is **parameterized per dataset** (default 1536,
matching OpenAI ``text-embedding-3-small`` / ``text-embedding-ada-002``).
The vector table is created lazily with the dataset's configured dimension —
a single static DDL cannot express a dynamic column width, so
:meth:`VectorStore.init` (called per dataset) issues the ``CREATE`` statement
with the concrete ``N``.
"""

from evoflow.knowledge.vector.base import RecallHit, VectorStoreError
from evoflow.knowledge.vector.sqlite_vec import VectorStore

__all__ = ["VectorStore", "VectorStoreError", "RecallHit"]
