"""Shared types/constants for the knowledge-base vector stores.

Kept dependency-free so both backends (:mod:`.sqlite_vec` and
:mod:`.pgvector_store`) can import from here without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_EMBEDDING_DIM = 1536
VECTORS_TABLE_PREFIX = "evoflow_kb_vectors_"


class VectorStoreError(RuntimeError):
    """Raised on vector-store misuse (extension missing, bad dims, ...)."""


def vectors_table_for(dataset_id: str) -> str:
    """Per-dataset table name (``evoflow_kb_vectors_<dataset_id>``).

    Each dataset gets its own table so datasets with different embedding
    dimensions can coexist (vector column width is fixed per table).
    """
    return f"{VECTORS_TABLE_PREFIX}{dataset_id}"


@dataclass(frozen=True)
class RecallHit:
    """A single recall result row."""

    chunk_id: str
    distance: float
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {"chunk_id": self.chunk_id, "distance": self.distance, "score": self.score}
