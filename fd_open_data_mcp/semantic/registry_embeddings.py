"""Write path for registry-indicator embeddings (mcp-search-engine-overhaul 4.1/4.2).

The ``registry_entries`` table is owned by the unified-indicator-registry
pipeline and lives in the same database this service reads; this module only
writes the derived ``registry_indicator_embeddings`` table next to it (raw
SQL, no ORM model — mirroring ``semantic/registry_catalog.py``). It provides:

  - ``build_embed_text`` — the canonical ``"name_zh | name_en | semantic_code"``
    corpus text (empty segments skipped; an all-empty row yields ``None``);
  - ``reembed_entries`` — the incremental hook to call after registry
    upserts. There is no registry upsert in THIS repo (the writer lives in
    the unified-indicator-registry pipeline), so this is exposed as a
    standalone-callable API for that pipeline;
  - ``vector_column_exists`` — probes for the pgvector ``embedding_vec``
    column so the JSON-only path stays usable until migration 3.2 lands.

The ``verified`` flag is deliberately NOT cached here: queries JOIN
``registry_entries.verified`` live (spec semantic-search — snapshot caches of
``verified`` go stale; the public path never trusts them).
"""
from __future__ import annotations

import datetime
import json
import logging
from typing import Any, Sequence

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.orm import Session

from fd_open_data_mcp.embeddings.model import MODEL_NAME, get_model

logger = logging.getLogger(__name__)

REGISTRY_TABLE = "registry_entries"
EMBEDDINGS_TABLE = "registry_indicator_embeddings"

_UPSERT_SQL = text(f"""
    INSERT INTO {EMBEDDINGS_TABLE}
        (registry_entry_id, embedding, embedded_text, model, updated_at)
    VALUES (:registry_entry_id, :embedding, :embedded_text, :model, :updated_at)
    ON CONFLICT (model, registry_entry_id) DO UPDATE SET
        embedding = :embedding,
        embedded_text = :embedded_text,
        updated_at = :updated_at
""")

# PG-only variant: also maintains the pgvector column when it exists (design
# D4 migration 3.2). CAST(:vec AS vector) — never the bare ::cast, so the
# statement parses identically for logging / analysis on any dialect.
_UPSERT_WITH_VECTOR_SQL = text(f"""
    INSERT INTO {EMBEDDINGS_TABLE}
        (registry_entry_id, embedding, embedded_text, model, updated_at, embedding_vec)
    VALUES (:registry_entry_id, :embedding, :embedded_text, :model, :updated_at,
            CAST(:vec AS vector))
    ON CONFLICT (model, registry_entry_id) DO UPDATE SET
        embedding = :embedding,
        embedded_text = :embedded_text,
        updated_at = :updated_at,
        embedding_vec = CAST(:vec AS vector)
""")


def build_embed_text(name_zh: Any, name_en: Any, semantic_code: Any) -> str | None:
    """Corpus text for one registry row: non-empty fields joined by ' | '.

    Empty / whitespace-only / NULL segments are skipped; a row whose three
    fields are all empty yields ``None`` (nothing to embed — the caller
    counts and skips it).
    """
    segments = [
        str(value).strip()
        for value in (name_zh, name_en, semantic_code)
        if value is not None and str(value).strip()
    ]
    if not segments:
        return None
    return " | ".join(segments)


def tables_present(session: Session) -> bool:
    """True when both the registry and its embeddings table exist."""
    inspector = inspect(session.get_bind())
    return inspector.has_table(REGISTRY_TABLE) and inspector.has_table(EMBEDDINGS_TABLE)


def vector_column_exists(session: Session) -> bool:
    """True when the pgvector ``embedding_vec`` column exists (PG only)."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    row = session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :table_name AND column_name = 'embedding_vec' "
            "LIMIT 1"
        ),
        {"table_name": EMBEDDINGS_TABLE},
    ).first()
    return row is not None


def upsert_embedding(
    session: Session,
    registry_entry_id: int,
    vector: Sequence[float],
    embedded_text: str,
    model: str = MODEL_NAME,
    with_vector_column: bool = False,
) -> None:
    """Insert or refresh one embedding row (idempotent on model+entry).

    The JSON serialization is the portable canonical form: PG stores it into
    JSONB (unknown-type parameter coerced), SQLite into TEXT.
    """
    payload = json.dumps([float(x) for x in vector])
    params = {
        "registry_entry_id": int(registry_entry_id),
        "embedding": payload,
        "embedded_text": embedded_text,
        "model": model,
        "updated_at": datetime.datetime.now(),
    }
    stmt = _UPSERT_SQL
    if with_vector_column:
        params["vec"] = payload
        stmt = _UPSERT_WITH_VECTOR_SQL
    session.execute(stmt, params)


def fetch_unembedded_entries(
    session: Session, model: str = MODEL_NAME, after_id: int = 0, batch_size: int = 256,
) -> list[dict]:
    """Registry rows with no embedding row for ``model`` yet (keyset-paged).

    Resume-by-rerun design: the batch job keeps re-asking this question, so a
    crashed run needs no journal — the next run simply picks up the rows that
    are still unembedded.
    """
    rows = session.execute(
        text(f"""
            SELECT re.id, re.name_zh, re.name_en, re.semantic_code
            FROM {REGISTRY_TABLE} re
            LEFT JOIN {EMBEDDINGS_TABLE} x
                ON x.registry_entry_id = re.id AND x.model = :model
            WHERE x.id IS NULL AND re.id > :after_id
            ORDER BY re.id
            LIMIT :limit
        """),
        {"model": model, "after_id": int(after_id), "limit": int(batch_size)},
    )
    return [dict(row) for row in rows.mappings()]


def count_unembedded_entries(session: Session, model: str = MODEL_NAME) -> int:
    """Rows still lacking an embedding for ``model`` AND carrying some text —
    i.e. the rows a rerun will actually pick up (all-empty rows are counted
    separately by ``count_unembeddable_entries``)."""
    return session.execute(
        text(f"""
            SELECT COUNT(*)
            FROM {REGISTRY_TABLE} re
            LEFT JOIN {EMBEDDINGS_TABLE} x
                ON x.registry_entry_id = re.id AND x.model = :model
            WHERE x.id IS NULL
                AND (COALESCE(re.name_zh, '') <> ''
                     OR COALESCE(re.name_en, '') <> ''
                     OR COALESCE(re.semantic_code, '') <> '')
        """),
        {"model": model},
    ).scalar_one()


def count_unembeddable_entries(session: Session, model: str = MODEL_NAME) -> int:
    """Unembedded rows whose three text fields are all empty (never embeddable)."""
    return session.execute(
        text(f"""
            SELECT COUNT(*)
            FROM {REGISTRY_TABLE} re
            LEFT JOIN {EMBEDDINGS_TABLE} x
                ON x.registry_entry_id = re.id AND x.model = :model
            WHERE x.id IS NULL
                AND COALESCE(re.name_zh, '') = ''
                AND COALESCE(re.name_en, '') = ''
                AND COALESCE(re.semantic_code, '') = ''
        """),
        {"model": model},
    ).scalar_one()


def count_embeddings(session: Session, model: str = MODEL_NAME) -> int:
    """Total embedding rows stored for ``model``."""
    return session.execute(
        text(f"SELECT COUNT(*) FROM {EMBEDDINGS_TABLE} WHERE model = :model"),
        {"model": model},
    ).scalar_one()


def reembed_entries(
    session: Session, entry_ids: Sequence[int], model_name: str = MODEL_NAME,
) -> dict[str, int]:
    """Incremental hook: (re-)embed the given registry entries — UPSERT.

    The unified-indicator-registry pipeline calls this after upserting
    ``registry_entries`` rows; idempotent via ON CONFLICT (model,
    registry_entry_id). Rows with no text at all are counted and skipped;
    unknown ids are reported as ``missing``. Invalidates the search result
    caches so fresh embeddings are served immediately.
    """
    ids = list(dict.fromkeys(int(i) for i in entry_ids))  # dedupe, keep order
    result = {"requested": len(ids), "embedded": 0, "skipped_empty": 0, "missing": 0}
    if not ids:
        return result

    if not tables_present(session):
        logger.info(
            "registry tables %r/%r not present; nothing to re-embed",
            REGISTRY_TABLE, EMBEDDINGS_TABLE,
        )
        result["missing"] = len(ids)
        return result

    rows: list[dict] = []
    for start in range(0, len(ids), 500):  # bounded expanding-IN chunks
        chunk = ids[start:start + 500]
        stmt = text(
            f"SELECT id, name_zh, name_en, semantic_code FROM {REGISTRY_TABLE} "
            "WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        rows.extend(
            dict(row) for row in session.execute(stmt, {"ids": chunk}).mappings()
        )
    found = {row["id"] for row in rows}
    result["missing"] = len([i for i in ids if i not in found])

    work: list[tuple[int, str]] = []
    for row in rows:
        entry_text = build_embed_text(row["name_zh"], row["name_en"], row["semantic_code"])
        if entry_text is None:
            result["skipped_empty"] += 1
            continue
        work.append((row["id"], entry_text))

    if work:
        model = get_model()
        vectors = model.encode([entry_text for _, entry_text in work])
        with_vector = vector_column_exists(session)
        for (entry_id, entry_text), vector in zip(work, vectors):
            upsert_embedding(
                session, entry_id, vector, entry_text,
                model=model_name, with_vector_column=with_vector,
            )
        session.commit()
    result["embedded"] = len(work)
    logger.info("reembed_entries: %s", result)

    # Stored embeddings changed: cached search results are stale.
    try:
        from fd_open_data_mcp import engines
        engines.invalidate_searches()
    except Exception:  # pragma: no cover — best-effort cache invalidation
        logger.debug("search-cache invalidation skipped", exc_info=True)
    return result
