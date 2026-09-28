"""Registry-indicator semantic retrieval (mcp-search-engine-overhaul 4.3).

Serves the unified indicator registry as a search corpus next to local
concepts (spec semantic-search 「语料覆盖」):

  - verified registry-only indicators are discoverable by natural-language
    queries, marked ``result_type: "registry"`` with their ``source_db``;
  - unverified entries are NEVER served — the ``verified`` filter is a live
    JOIN against ``registry_entries.verified`` on every query (a snapshot
    cached at embed time would go stale the moment an entry is verified or
    revoked).

Backend selection reads ``FD_MCP_VECTOR_BACKEND`` (json default) with the
same semantics as ``fd_open_data_mcp/vector_backend.py`` (developed in
parallel — deliberately not imported here):

  - ``json`` / ``matrix`` (and anything unrecognised): full-cosine scan over
    the JSONB embeddings, computed in-process. SQLite-compatible; ``matrix``
    is the transitional in-process cache of the same embeddings, so results
    are equivalent by construction until that backend lands;
  - ``pgvector``: ``ORDER BY embedding_vec <=> CAST(:qv AS vector)`` inside
    PostgreSQL — used only when the column actually exists (probed via
    information_schema), otherwise one warning is logged and the JSON path
    serves the query.

Fail-soft like ``registry_catalog``: a missing table (SQLite dev databases,
or PG before the registry DDL) yields ``[]`` after a single log line.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from fd_open_data_mcp.embeddings.model import MODEL_NAME, get_model
from fd_open_data_mcp.semantic.registry_embeddings import (
    tables_present,
    vector_column_exists,
)

logger = logging.getLogger(__name__)

# At least one of the three corpus fields must be non-empty — matches the
# embed job (all-empty rows were never embedded) and keeps the scan honest.
_NON_EMPTY_CLAUSE = (
    "(COALESCE(re.name_zh, '') <> '' OR COALESCE(re.name_en, '') <> '' "
    "OR COALESCE(re.semantic_code, '') <> '')"
)


def _backend() -> str:
    """FD_MCP_VECTOR_BACKEND, normalised (json default — same contract as
    fd_open_data_mcp.vector_backend, developed in parallel)."""
    return os.environ.get("FD_MCP_VECTOR_BACKEND", "json").strip().lower()


def _hit(row: Any, similarity: Any) -> dict:
    return {
        "semantic_code": row.semantic_code,
        "name_zh": row.name_zh,
        "name_en": row.name_en,
        "source_db": row.source_db,
        "similarity": float(similarity),
        "result_type": "registry",
    }


def _search_json(session: Session, query: str, limit: int) -> list[dict]:
    """Portable path: cosine similarity over the stored JSON embeddings."""
    model = get_model()
    query_vec = np.asarray(model.encode([query])[0], dtype=np.float32)
    query_norm = float(np.linalg.norm(query_vec))
    if query_norm == 0.0:
        return []

    rows = session.execute(
        text(f"""
            SELECT re.semantic_code, re.name_zh, re.name_en, re.source_db, x.embedding
            FROM registry_indicator_embeddings x
            JOIN registry_entries re ON re.id = x.registry_entry_id
            WHERE x.model = :model AND re.verified AND {_NON_EMPTY_CLAUSE}
        """),
        {"model": MODEL_NAME},
    )

    hits: list[dict] = []
    for row in rows:
        stored = row.embedding
        if isinstance(stored, str):
            try:
                stored = json.loads(stored)
            except json.JSONDecodeError:
                continue
        vec = np.asarray(stored, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm == 0.0 or vec.shape != query_vec.shape:
            continue
        hits.append(_hit(row, float(np.dot(query_vec, vec) / (query_norm * norm))))

    hits.sort(key=lambda h: h["similarity"], reverse=True)
    return hits[:limit]


def _search_pgvector(session: Session, query: str, limit: int) -> list[dict]:
    """Indexed path: cosine-distance ordering inside PostgreSQL."""
    model = get_model()
    qvec = json.dumps([float(x) for x in model.encode([query])[0]])
    # Wider HNSW beam: default ef_search=40 misses exact/near neighbors in
    # tight clusters (same fix as vector_backend._set_hnsw_ef).
    try:
        session.execute(text(
            f"SET hnsw.ef_search = {os.environ.get('FD_MCP_HNSW_EF_SEARCH', '200')}"))
    except Exception:  # noqa: BLE001 - non-PG/missing GUC: default still works
        pass
    stmt = text(f"""
        SELECT re.semantic_code, re.name_zh, re.name_en, re.source_db,
               1 - (x.embedding_vec <=> CAST(:qvec AS vector)) AS similarity
        FROM registry_indicator_embeddings x
        JOIN registry_entries re ON re.id = x.registry_entry_id
        WHERE x.model = :model AND re.verified AND {_NON_EMPTY_CLAUSE}
        ORDER BY x.embedding_vec <=> CAST(:qvec AS vector)
        LIMIT :limit
    """)
    rows = session.execute(
        stmt, {"model": MODEL_NAME, "qvec": qvec, "limit": int(limit)},
    )
    return [_hit(row, row.similarity) for row in rows]


def search_registry(
    query: str, limit: int = 20, session: Session | None = None,
) -> list[dict]:
    """Semantic search over verified registry entries.

    Returns ``{semantic_code, name_zh, name_en, source_db, similarity,
    result_type: "registry"}`` sorted by similarity desc. ``verified`` is
    evaluated live via the JOIN — never a snapshot from embed time. When
    ``session`` is omitted, one is opened against the configured database.
    Fail-soft: missing tables or an unreadable corpus return ``[]``.
    """
    if session is None:
        from fd_open_data_mcp import db as dbmod
        session = dbmod.get_database().get_session()
        own_session = True
    else:
        own_session = False
    try:
        if not tables_present(session):
            logger.debug(
                "registry corpus tables not present; registry hits skipped",
            )
            return []

        backend = _backend()
        if backend == "pgvector":
            if vector_column_exists(session):
                try:
                    return _search_pgvector(session, query, limit)
                except (OperationalError, ProgrammingError) as exc:
                    logger.warning(
                        "pgvector registry search failed (%s); "
                        "falling back to the JSON cosine scan", exc,
                    )
            else:
                logger.warning(
                    "FD_MCP_VECTOR_BACKEND=pgvector but the embedding_vec "
                    "column is absent; serving via the JSON cosine scan",
                )
        # json / matrix / unknown backends: equivalent full-cosine scan.
        return _search_json(session, query, limit)
    except (OperationalError, ProgrammingError) as exc:
        logger.info("registry corpus unreadable (%s); registry hits skipped", exc)
        return []
    finally:
        if own_session:
            session.close()


def merge_registry_hits(
    local_results: list[dict],
    registry_hits: list[dict],
    limit: int | None = None,
    key=None,
) -> list[dict]:
    """Merge registry hits into a local result list (local wins collisions).

    Dedup rule (spec「语料覆盖」): a registry hit whose ``semantic_code``
    equals an already-returned local ``code`` is dropped — the local concept
    takes priority. Survivors are appended after the local results; when
    ``key`` is given the merged list is re-sorted with it (descending) before
    the optional ``limit`` truncation, so registry-only indicators compete
    for slots on the same ranking scale.
    """
    local_codes = {r.get("code") for r in local_results if r.get("code")}
    fresh = [h for h in registry_hits if h.get("semantic_code") not in local_codes]
    merged = list(local_results) + fresh
    if key is not None:
        merged.sort(key=key, reverse=True)
    if limit is not None:
        merged = merged[:limit]
    return merged
