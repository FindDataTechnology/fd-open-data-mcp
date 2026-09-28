"""Pluggable similarity backends for semantic candidate retrieval.

Three interchangeable backends behind one dispatch (mcp-search-engine-overhaul
design D4/D6; spec semantic-search "vector similarity is served by an indexed
vector column" + "transitional cache serves equivalent results"):

- ``json`` (default): the legacy path — SELECT every embedding as JSON text and
  dot-product in Python. Byte-for-byte the pre-migration behavior; runs
  everywhere, including SQLite dev databases, and is the rollback target.
- ``matrix``: transitional in-process cache — the corpus is loaded once as a
  numpy float32 matrix plus row metadata and served as matrix-vector products
  until ``VECTOR_MATRIX_TTL`` expires or a write tool calls
  ``engines.invalidate_searches``. Also SQLite-compatible (it loads the JSON
  column, never the vector column).
- ``pgvector``: similarity executes inside PostgreSQL via the indexed ``<=>``
  cosine-distance operator on ``embedding_vec vector(384)`` columns; a top-K
  prefilter is then ranked with the shared binding-aware ordering. PostgreSQL
  only — refuses to run against SQLite (deployment guard).

Selection is the ``FD_MCP_VECTOR_BACKEND`` environment variable
(json|matrix|pgvector), read on every call so tests can monkeypatch it. An
unknown value fails loudly instead of silently degrading to a full scan.
"""
from __future__ import annotations

import json
import os
import threading
import time

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from fd_open_data_mcp.embeddings.model import MODEL_NAME

_BACKEND_ENV = "FD_MCP_VECTOR_BACKEND"
_BACKENDS = ("json", "matrix", "pgvector")

_MATRIX_TTL_ENV = "VECTOR_MATRIX_TTL"
_DEFAULT_MATRIX_TTL = 300.0

# How many raw-similarity leaders the matrix/pgvector backends prefetch before
# the binding-aware ranking (bound concepts can jump an unbound one by up to
# one 0.05 bucket, so 3x headroom plus a floor keeps the final top-`limit`
# identical on any realistic corpus).
_K_FACTOR = 3
_K_FLOOR = 50


def get_backend_name() -> str:
    """Current backend name from the environment (default ``json``)."""
    name = os.environ.get(_BACKEND_ENV, "json").strip().lower()
    if name not in _BACKENDS:
        raise RuntimeError(
            f"{_BACKEND_ENV}={name!r} is not one of {list(_BACKENDS)}; "
            "set it to 'json' (legacy full scan), 'matrix' (transitional "
            "in-process cache) or 'pgvector' (indexed vector column)"
        )
    return name


def _matrix_ttl() -> float:
    raw = os.environ.get(_MATRIX_TTL_ENV, str(_DEFAULT_MATRIX_TTL))
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_MATRIX_TTL


def _prefetch_k(limit: int) -> int:
    return max(_K_FACTOR * limit, _K_FLOOR)


def _parse_embedding(raw):
    """Stored embedding (JSON string / JSONB list / sequence) -> float list."""
    if isinstance(raw, str):
        return json.loads(raw)
    return [float(x) for x in raw]


def _parse_metadata(raw):
    """metadata_json column value -> dict, tolerating junk text."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw


def _cosine_similarity(vec1, vec2) -> float:
    """Cosine similarity with the zero-vector guard of the entity path."""
    a = np.array(vec1)
    b = np.array(vec2)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _rank_key(candidate: dict):
    """Binding-aware ranking shared by every backend (spec entity-semantic-search).

    Bucket similarity into 0.05-wide bins; within a bin bound concepts rank
    first, then exact similarity. Concepts >0.05 apart are ordered by bin.
    """
    sim = candidate["similarity"]
    return (round(sim / 0.05), 1 if candidate["has_binding"] else 0, sim)


def _bound_concept_ids(session) -> set:
    """Concepts with at least one binding (queried fresh, like the json path)."""
    return {
        row[0]
        for row in session.execute(text("SELECT DISTINCT concept_id FROM concept_bindings"))
    }


def _require_postgresql(session) -> None:
    """pgvector backend deployment guard: refuse SQLite outright."""
    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        raise RuntimeError(
            "FD_MCP_VECTOR_BACKEND=pgvector requires PostgreSQL with the "
            "pgvector extension (embedding_vec vector columns + HNSW index); "
            f"got a {bind.dialect.name} database. Use the 'json' or 'matrix' "
            "backend on SQLite, or point FD_OPEN_DATA_MCP_DATABASE_URL at the "
            "migrated PostgreSQL instance."
        )


def _vector_literal(query_embedding) -> str:
    """pgvector bind value: '[0.1,0.2,...]' literal accepted by CAST(... AS vector)."""
    return str([float(x) for x in query_embedding])


# ─── json backend (legacy behavior, ported verbatim) ────────────────────────

def _concepts_json(session, query_embedding, entity_type, frequency,
                   include_unbound, model, limit):
    filter_clauses = []
    params = {"model": model}

    if entity_type:
        filter_clauses.append("c.entity_type = :entity_type")
        params["entity_type"] = entity_type

    if frequency:
        filter_clauses.append("c.frequency = :frequency")
        params["frequency"] = frequency

    # Exclude deprecated concepts (spec entity-semantic-search: discovery
    # excludes deprecated).
    filter_clauses.append("COALESCE(c.deprecated, false) = false")

    where_clause = " AND ".join(filter_clauses) if filter_clauses else "1=1"

    sql = f"""
        SELECT
            c.id, c.code, c.name_en, c.name_zh, c.category, c.unit,
            c.measure, c.frequency, c.entity_type, c.source,
            ce.embedding
        FROM concepts c
        JOIN concept_embeddings ce ON c.id = ce.concept_id
        WHERE ce.model = :model
            AND {where_clause}
    """

    result = session.execute(text(sql), params)
    bound_ids = _bound_concept_ids(session)

    query_array = np.array(query_embedding, dtype=np.float32)
    candidates = []
    for row in result:
        embedding_list = _parse_embedding(row.embedding)

        # Cosine similarity for normalized vectors = dot product
        embedding_array = np.array(embedding_list, dtype=np.float32)
        similarity = float(np.dot(query_array, embedding_array))

        has_binding = row.id in bound_ids
        if not has_binding and not include_unbound:
            continue

        candidates.append({
            "id": row.id,
            "code": row.code,
            "name_en": row.name_en,
            "name_zh": row.name_zh,
            "category": row.category,
            "unit": row.unit,
            "measure": row.measure,
            "frequency": row.frequency,
            "entity_type": row.entity_type,
            "source": row.source,
            "similarity": similarity,
            "has_binding": has_binding,
        })

    candidates.sort(key=_rank_key, reverse=True)
    return candidates[:limit]


def _entities_json(session, query_embedding, entity_type, model, limit):
    sql = """
        SELECT
            e.id,
            e.entity_type,
            e.code,
            e.name_en,
            e.name_zh,
            e.metadata_json,
            ee.embedding
        FROM entities e
        JOIN entity_embeddings ee ON e.id = ee.entity_id
        WHERE ee.model = :model
    """

    params = {"model": model}

    if entity_type:
        sql += " AND e.entity_type = :entity_type"
        params["entity_type"] = entity_type

    result = session.execute(text(sql), params)

    results = []
    for row in result:
        embedding = _parse_embedding(row.embedding)
        similarity = _cosine_similarity(query_embedding, embedding)

        results.append({
            "id": row.id,
            "entity_type": row.entity_type,
            "code": row.code,
            "name_en": row.name_en,
            "name_zh": row.name_zh,
            "metadata": _parse_metadata(row.metadata_json),
            "similarity": float(similarity),
        })

    # Sort by similarity and limit
    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:limit]


# ─── matrix backend (transitional in-process cache, design D6) ──────────────

_matrix_lock = threading.Lock()
# (kind, model, database URL) -> loaded matrix + row metadata
_matrix_caches: dict[tuple[str, str, str], "_MatrixEntry"] = {}


class _MatrixEntry:
    """One loaded corpus: ids + float32 matrix + per-row metadata."""

    __slots__ = ("loaded_at", "ids", "matrix", "meta")

    def __init__(self, ids, matrix, meta):
        self.loaded_at = time.monotonic()
        self.ids = ids
        self.matrix = matrix
        self.meta = meta

    def expired(self) -> bool:
        return (time.monotonic() - self.loaded_at) >= _matrix_ttl()


def invalidate_matrix_cache() -> None:
    """Drop every cached matrix (write tools / tests / DB switch).

    Wired into ``engines.invalidate_searches`` so concept/embedding writes do
    not wait out the TTL.
    """
    with _matrix_lock:
        _matrix_caches.clear()


def _get_matrix(session, kind: str, model: str, loader) -> "_MatrixEntry":
    key = (kind, model, str(session.get_bind().url))
    entry = _matrix_caches.get(key)
    if entry is not None and not entry.expired():
        return entry
    with _matrix_lock:
        entry = _matrix_caches.get(key)
        if entry is None or entry.expired():
            entry = loader(session, model)
            _matrix_caches[key] = entry
        return entry


def _load_concept_matrix(session, model):
    sql = text("""
        SELECT
            c.id, c.code, c.name_en, c.name_zh, c.category, c.unit,
            c.measure, c.frequency, c.entity_type, c.source, c.deprecated,
            ce.embedding
        FROM concepts c
        JOIN concept_embeddings ce ON c.id = ce.concept_id
        WHERE ce.model = :model
    """)
    rows = session.execute(sql, {"model": model}).fetchall()
    ids, vectors, meta = [], [], []
    for row in rows:
        ids.append(row.id)
        vectors.append(_parse_embedding(row.embedding))
        meta.append({
            "code": row.code,
            "name_en": row.name_en,
            "name_zh": row.name_zh,
            "category": row.category,
            "unit": row.unit,
            "measure": row.measure,
            "frequency": row.frequency,
            "entity_type": row.entity_type,
            "source": row.source,
            "deprecated": bool(row.deprecated) if row.deprecated is not None else False,
        })
    matrix = (
        np.array(vectors, dtype=np.float32)
        if vectors else np.zeros((0, 0), dtype=np.float32)
    )
    return _MatrixEntry(ids, matrix, meta)


def _load_entity_matrix(session, model):
    sql = text("""
        SELECT
            e.id, e.entity_type, e.code, e.name_en, e.name_zh,
            e.metadata_json, ee.embedding
        FROM entities e
        JOIN entity_embeddings ee ON e.id = ee.entity_id
        WHERE ee.model = :model
    """)
    rows = session.execute(sql, {"model": model}).fetchall()
    ids, vectors, meta = [], [], []
    for row in rows:
        ids.append(row.id)
        vectors.append(_parse_embedding(row.embedding))
        meta.append({
            "entity_type": row.entity_type,
            "code": row.code,
            "name_en": row.name_en,
            "name_zh": row.name_zh,
            "metadata": _parse_metadata(row.metadata_json),
        })
    matrix = (
        np.array(vectors, dtype=np.float32)
        if vectors else np.zeros((0, 0), dtype=np.float32)
    )
    return _MatrixEntry(ids, matrix, meta)


def _concepts_matrix(session, query_embedding, entity_type, frequency,
                     include_unbound, model, limit):
    cache = _get_matrix(session, "concept", model, _load_concept_matrix)

    bound_ids = _bound_concept_ids(session)

    # Row-level filters applied at query time so one cached matrix serves
    # every filter combination (same predicates as the json backend's SQL).
    eligible = [
        i for i, m in enumerate(cache.meta)
        if (entity_type is None or m["entity_type"] == entity_type)
        and (frequency is None or m["frequency"] == frequency)
        and not m["deprecated"]
        and (include_unbound or cache.ids[i] in bound_ids)
    ]
    if not eligible:
        return []

    sub = cache.matrix[eligible]
    sims = sub @ np.array(query_embedding, dtype=np.float32)

    # Top-K by raw similarity, then the shared binding-aware ranking.
    order = np.argsort(-sims, kind="stable")[: _prefetch_k(limit)]
    candidates = []
    for pos in order:
        i = eligible[pos]
        m = cache.meta[i]
        has_binding = cache.ids[i] in bound_ids
        candidates.append({
            "id": cache.ids[i],
            "code": m["code"],
            "name_en": m["name_en"],
            "name_zh": m["name_zh"],
            "category": m["category"],
            "unit": m["unit"],
            "measure": m["measure"],
            "frequency": m["frequency"],
            "entity_type": m["entity_type"],
            "source": m["source"],
            "similarity": float(sims[pos]),
            "has_binding": has_binding,
        })

    candidates.sort(key=_rank_key, reverse=True)
    return candidates[:limit]


def _entities_matrix(session, query_embedding, entity_type, model, limit):
    cache = _get_matrix(session, "entity", model, _load_entity_matrix)

    eligible = [
        i for i, m in enumerate(cache.meta)
        if entity_type is None or m["entity_type"] == entity_type
    ]
    if not eligible:
        return []

    sub = cache.matrix[eligible]
    q = np.array(query_embedding, dtype=np.float64)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        sims = np.zeros(len(eligible), dtype=np.float64)
    else:
        row_norms = np.linalg.norm(sub, axis=1).astype(np.float64)
        dots = sub @ q  # float32 matrix x float64 vector -> float64
        sims = np.divide(
            dots, row_norms * q_norm,
            out=np.zeros(len(eligible), dtype=np.float64),
            where=row_norms > 0,
        )

    order = np.argsort(-sims, kind="stable")[: _prefetch_k(limit)]
    results = []
    for pos in order:
        i = eligible[pos]
        m = cache.meta[i]
        results.append({
            "id": cache.ids[i],
            "entity_type": m["entity_type"],
            "code": m["code"],
            "name_en": m["name_en"],
            "name_zh": m["name_zh"],
            "metadata": m["metadata"],
            "similarity": float(sims[pos]),
        })

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:limit]


# ─── pgvector backend (indexed in-database similarity, design D4) ───────────

# HNSW search beam width. pgvector's default ef_search=40 loses exact/near
# neighbors inside tight duplicate clusters (verified on the entity corpus:
# distance-0 self-matches missed at ef<=100, exact at ef=200), which broke
# dual-read equivalence. 200 restores exact top-K at this corpus size.
def _hnsw_ef_search() -> int:
    return int(os.environ.get("FD_MCP_HNSW_EF_SEARCH", "200"))


def _set_hnsw_ef(session) -> None:
    try:
        session.execute(text(f"SET hnsw.ef_search = {_hnsw_ef_search()}"))
    except Exception:  # noqa: BLE001 - non-PG or missing GUC: default still works
        pass


def _concepts_pgvector(session, query_embedding, entity_type, frequency,
                       include_unbound, model, limit):
    _require_postgresql(session)
    _set_hnsw_ef(session)

    params = {
        "model": model,
        "qv": _vector_literal(query_embedding),
        "k": _prefetch_k(limit),
    }

    filters = [
        "ce.embedding_vec IS NOT NULL",
        "COALESCE(c.deprecated, false) = false",
    ]
    if entity_type:
        filters.append("c.entity_type = :entity_type")
        params["entity_type"] = entity_type
    if frequency:
        filters.append("c.frequency = :frequency")
        params["frequency"] = frequency
    if not include_unbound:
        # Same meaning as the json path skipping unbound candidates.
        filters.append(
            "EXISTS (SELECT 1 FROM concept_bindings cb WHERE cb.concept_id = c.id)"
        )

    sql = f"""
        SELECT
            c.id, c.code, c.name_en, c.name_zh, c.category, c.unit,
            c.measure, c.frequency, c.entity_type, c.source,
            1 - (ce.embedding_vec <=> CAST(:qv AS vector)) AS similarity,
            EXISTS (SELECT 1 FROM concept_bindings cb WHERE cb.concept_id = c.id)
                AS has_binding
        FROM concept_embeddings ce
        JOIN concepts c ON c.id = ce.concept_id
        WHERE ce.model = :model
            AND {" AND ".join(filters)}
        ORDER BY ce.embedding_vec <=> CAST(:qv AS vector)
        LIMIT :k
    """

    candidates = [
        {
            "id": row.id,
            "code": row.code,
            "name_en": row.name_en,
            "name_zh": row.name_zh,
            "category": row.category,
            "unit": row.unit,
            "measure": row.measure,
            "frequency": row.frequency,
            "entity_type": row.entity_type,
            "source": row.source,
            "similarity": float(row.similarity),
            "has_binding": bool(row.has_binding),
        }
        for row in session.execute(text(sql), params)
    ]

    candidates.sort(key=_rank_key, reverse=True)
    return candidates[:limit]


def _entities_pgvector(session, query_embedding, entity_type, model, limit):
    _set_hnsw_ef(session)
    _require_postgresql(session)

    params = {
        "model": model,
        "qv": _vector_literal(query_embedding),
        "k": _prefetch_k(limit),
    }

    filters = ["ee.embedding_vec IS NOT NULL"]
    if entity_type:
        filters.append("e.entity_type = :entity_type")
        params["entity_type"] = entity_type

    sql = f"""
        SELECT
            e.id, e.entity_type, e.code, e.name_en, e.name_zh,
            e.metadata_json,
            1 - (ee.embedding_vec <=> CAST(:qv AS vector)) AS similarity
        FROM entity_embeddings ee
        JOIN entities e ON e.id = ee.entity_id
        WHERE ee.model = :model
            AND {" AND ".join(filters)}
        ORDER BY ee.embedding_vec <=> CAST(:qv AS vector)
        LIMIT :k
    """

    results = [
        {
            "id": row.id,
            "entity_type": row.entity_type,
            "code": row.code,
            "name_en": row.name_en,
            "name_zh": row.name_zh,
            "metadata": _parse_metadata(row.metadata_json),
            "similarity": float(row.similarity),
        }
        for row in session.execute(text(sql), params)
    ]

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:limit]


# ─── public dispatch ─────────────────────────────────────────────────────────

def search_concept_candidates(
    session: Session,
    query_embedding: list[float],
    entity_type: str | None = None,
    limit: int = 20,
    include_unbound: bool = True,
    model: str = MODEL_NAME,
    frequency: str | None = None,
) -> list[dict]:
    """Concept candidates for a query embedding, ranked binding-aware.

    Field shape and ordering are identical across backends:
    id/code/name_en/name_zh/category/unit/measure/frequency/entity_type/source/
    similarity/has_binding, bucket-ranked (0.05 bins, bound first) and cut to
    ``limit``.
    """
    backend = get_backend_name()
    if backend == "json":
        return _concepts_json(
            session, query_embedding, entity_type, frequency,
            include_unbound, model, limit,
        )
    if backend == "matrix":
        return _concepts_matrix(
            session, query_embedding, entity_type, frequency,
            include_unbound, model, limit,
        )
    return _concepts_pgvector(
        session, query_embedding, entity_type, frequency,
        include_unbound, model, limit,
    )


def search_entity_candidates(
    session: Session,
    query_embedding: list[float],
    entity_type: str | None = None,
    limit: int = 20,
    model: str = MODEL_NAME,
) -> list[dict]:
    """Entity candidates for a query embedding, similarity-ranked.

    Field shape matches ``EntitySemanticSearch.search``:
    id/entity_type/code/name_en/name_zh/metadata/similarity.
    """
    backend = get_backend_name()
    if backend == "json":
        return _entities_json(session, query_embedding, entity_type, model, limit)
    if backend == "matrix":
        return _entities_matrix(session, query_embedding, entity_type, model, limit)
    return _entities_pgvector(session, query_embedding, entity_type, model, limit)
