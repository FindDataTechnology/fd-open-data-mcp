"""mcp-search-engine-overhaul 3.3 + 3.4: vector-backend abstraction equivalence.

The three similarity backends (json legacy scan / transitional in-process
matrix / pgvector indexed column) must return the same ranked results for the
same query (spec semantic-search "transitional cache serves equivalent
results"), the flag must default safely, pgvector must refuse SQLite, and the
matrix cache must honor VECTOR_MATRIX_TTL plus engines.invalidate_searches().

json-vs-matrix equivalence runs on SQLite (both backends load the JSON
column). json-vs-pgvector equivalence needs a real PostgreSQL with the
pgvector extension and is skipped unless FD_MCP_VECTOR_TEST_DSN is set.
"""
from __future__ import annotations

import json
import os
import time

import pytest
from sqlalchemy import create_engine, event, text

from fd_open_data_mcp import engines
from fd_open_data_mcp import vector_backend
from fd_open_data_mcp.embeddings.model import MODEL_NAME, get_model

PG_DSN = os.environ.get("FD_MCP_VECTOR_TEST_DSN")

# (code, name_en, entity_type, frequency, deprecated, bound, embedded text)
_CONCEPTS = [
    ("CPI_YOY", "Consumer Price Index year-on-year inflation rate",
     "country", "monthly", False, True,
     "Consumer Price Index year-on-year inflation rate"),
    ("GDP_REAL_GROWTH", "Gross domestic product real growth rate",
     "country", "quarterly", False, True,
     "Gross domestic product real growth rate"),
    ("UNEMP_RATE", "Unemployment rate",
     "country", "monthly", False, False,
     "Unemployment rate"),
    ("STOCK_CLOSE", "Stock daily closing price",
     "stock", "daily", False, True,
     "Stock daily closing price"),
    ("CPI_SHADOW", "Consumer Price Index year-on-year inflation rate duplicate",
     "country", "monthly", True, True,
     "Consumer Price Index year-on-year inflation rate duplicate"),
    ("LEND_RATE", "Bank lending interest rate",
     "country", "quarterly", False, False,
     "Bank lending interest rate"),
]

# (entity_type, code, name_en, name_zh, metadata)
_ENTITIES = [
    ("country", "CN", "China", "中国", {"region": "asia"}),
    ("country", "US", "United States", "美国", {"region": "americas"}),
    ("stock", "AAPL", "Apple Inc. technology company", "苹果公司", {"exchange": "NASDAQ"}),
    ("industry", "SW_SOFTWARE", "Software industry", "软件行业", {"family": "software"}),
]


@pytest.fixture(autouse=True)
def _clean_vector_state(monkeypatch):
    """Every test starts on the default backend with an empty matrix cache."""
    monkeypatch.delenv("FD_MCP_VECTOR_BACKEND", raising=False)
    monkeypatch.delenv("VECTOR_MATRIX_TTL", raising=False)
    engines.reset_engines()
    vector_backend.invalidate_matrix_cache()
    yield
    vector_backend.invalidate_matrix_cache()
    engines.reset_engines()


def _seed_corpus(session) -> None:
    """Seed concepts/embeddings/bindings + entities/embeddings (raw SQL).

    Raw SQL (not the ORM) so the exact same seeding works against both the
    SQLite test database (conftest-built from the models) and the minimal
    pgvector schema the PG-only tests create.
    """
    model = get_model()

    # Binding target chain (sources -> functions -> columns) for FK integrity.
    # Every NOT NULL column is supplied explicitly: the ORM-backed SQLite
    # tables enforce them via ORM-level defaults, which raw INSERTs bypass.
    session.execute(text(
        "INSERT INTO sources (name, label) VALUES ('vec-eq-test', 'Vector Equivalence Test')"
    ))
    session.execute(text("""
        INSERT INTO functions
            (source_id, command, verified, scanner_mode, bulk_history, bulk_snapshot)
        VALUES (1, 'vec_eq_fn', true, 'upstream-curated', false, false)
    """))
    session.execute(text(
        'INSERT INTO "columns" (function_id, name, meaning) VALUES (1, \'value\', \'unknown\')'
    ))

    for code, name_en, etype, freq, deprecated, bound, emb_text in _CONCEPTS:
        session.execute(
            text("""
                INSERT INTO concepts
                    (code, name_en, name_zh, category, unit, measure,
                     frequency, entity_type, source, verified, deprecated)
                VALUES (:code, :name_en, NULL, NULL, NULL, '',
                        :frequency, :entity_type, 'vec-eq-test', true, :deprecated)
            """),
            {
                "code": code, "name_en": name_en, "frequency": freq,
                "entity_type": etype, "deprecated": deprecated,
            },
        )

    ids = {
        row.code: row.id
        for row in session.execute(text("SELECT id, code FROM concepts"))
    }
    for code, _name, _etype, _freq, _dep, bound, emb_text in _CONCEPTS:
        emb = model.encode([emb_text])[0].tolist()
        session.execute(
            text("""
                INSERT INTO concept_embeddings (concept_id, embedding, model)
                VALUES (:concept_id, :embedding, :model)
            """),
            {"concept_id": ids[code], "embedding": json.dumps(emb), "model": MODEL_NAME},
        )
        if bound:
            session.execute(
                text("""
                    INSERT INTO concept_bindings
                        (concept_id, column_id, confidence, provenance, reviewed)
                    VALUES (:concept_id, 1, 0.0, 'manual', true)
                """),
                {"concept_id": ids[code]},
            )

    for etype, code, name_en, name_zh, metadata in _ENTITIES:
        session.execute(
            text("""
                INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json)
                VALUES (:entity_type, :code, :name_en, :name_zh, :metadata)
            """),
            {
                "entity_type": etype, "code": code, "name_en": name_en,
                "name_zh": name_zh, "metadata": json.dumps(metadata),
            },
        )

    entity_ids = {
        row.code: row.id
        for row in session.execute(text("SELECT id, code FROM entities"))
    }
    for etype, code, name_en, _name_zh, _metadata in _ENTITIES:
        emb = model.encode([name_en])[0].tolist()
        session.execute(
            text("""
                INSERT INTO entity_embeddings (entity_id, embedding, model)
                VALUES (:entity_id, :embedding, :model)
            """),
            {"entity_id": entity_ids[code], "embedding": json.dumps(emb), "model": MODEL_NAME},
        )

    session.commit()


def _query_embedding(query: str) -> list[float]:
    return get_model().encode([query])[0].tolist()


def _assert_equivalent(expected: list[dict], actual: list[dict], tol: float = 1e-4) -> None:
    """Same rows in the same order; similarity within tol; all else identical."""
    assert [c["id"] for c in actual] == [c["id"] for c in expected], (
        f"id order differs:\n  expected {[c['id'] for c in expected]}\n"
        f"  actual   {[c['id'] for c in actual]}"
    )
    assert len(actual) == len(expected)
    for exp, act in zip(expected, actual):
        for key, value in exp.items():
            if key == "similarity":
                assert abs(act[key] - value) <= tol, (
                    f"similarity drift >{tol} for id {exp['id']}: {value} vs {act[key]}"
                )
            else:
                assert act[key] == value, (
                    f"field {key!r} differs for id {exp['id']}: {value!r} vs {act[key]!r}"
                )


# (query, entity_type, frequency, include_unbound)
_CONCEPT_QUERIES = [
    ("consumer price inflation", None, None, True),
    ("economic growth", "country", None, True),
    ("inflation rate", "country", "monthly", True),
    ("interest rate", None, None, False),
    ("stock price", "stock", None, True),
    ("labor market", None, "quarterly", True),
]


# ─── flag behavior ───────────────────────────────────────────────────────────

def test_backend_flag_defaults_to_json(monkeypatch):
    monkeypatch.delenv("FD_MCP_VECTOR_BACKEND", raising=False)
    assert vector_backend.get_backend_name() == "json"
    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "matrix")
    assert vector_backend.get_backend_name() == "matrix"
    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "PGVECTOR")  # normalized
    assert vector_backend.get_backend_name() == "pgvector"


def test_backend_flag_invalid_value_fails_loud(monkeypatch):
    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "redis")
    with pytest.raises(RuntimeError, match="redis"):
        vector_backend.get_backend_name()


def test_pgvector_backend_refuses_sqlite(session, monkeypatch):
    """Deployment guard: pgvector on SQLite raises instead of scanning."""
    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "pgvector")
    _seed_corpus(session)
    qv = _query_embedding("inflation")
    with pytest.raises(RuntimeError, match="pgvector"):
        vector_backend.search_concept_candidates(session, qv, limit=5, model=MODEL_NAME)
    with pytest.raises(RuntimeError, match="pgvector"):
        vector_backend.search_entity_candidates(session, qv, limit=5, model=MODEL_NAME)


# ─── json vs matrix equivalence on SQLite ────────────────────────────────────

@pytest.mark.parametrize("query,entity_type,frequency,include_unbound", _CONCEPT_QUERIES)
def test_concept_results_equivalent_json_vs_matrix(
    session, monkeypatch, query, entity_type, frequency, include_unbound
):
    _seed_corpus(session)
    qv = _query_embedding(query)

    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "json")
    expected = vector_backend.search_concept_candidates(
        session, qv, entity_type=entity_type, limit=3,
        include_unbound=include_unbound, model=MODEL_NAME, frequency=frequency,
    )

    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "matrix")
    actual = vector_backend.search_concept_candidates(
        session, qv, entity_type=entity_type, limit=3,
        include_unbound=include_unbound, model=MODEL_NAME, frequency=frequency,
    )

    _assert_equivalent(expected, actual)
    codes = {c["code"] for c in expected}
    assert "CPI_SHADOW" not in codes, "deprecated concept leaked into results"
    if not include_unbound:
        assert all(c["has_binding"] for c in expected)


@pytest.mark.parametrize("query,entity_type", [
    ("China", None),
    ("technology company", "stock"),
    ("software", "industry"),
    ("United States of America", "country"),
])
def test_entity_results_equivalent_json_vs_matrix(session, monkeypatch, query, entity_type):
    _seed_corpus(session)
    qv = _query_embedding(query)

    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "json")
    expected = vector_backend.search_entity_candidates(
        session, qv, entity_type=entity_type, limit=3, model=MODEL_NAME,
    )

    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "matrix")
    actual = vector_backend.search_entity_candidates(
        session, qv, entity_type=entity_type, limit=3, model=MODEL_NAME,
    )

    _assert_equivalent(expected, actual)
    assert expected, "entity search found nothing"


# ─── matrix cache lifecycle ──────────────────────────────────────────────────

def _embedding_table_query_counter(engine):
    """Count SELECTs that load embeddings (matrix (re)loads touch these)."""
    counter = {"n": 0}

    def _incr(*args):
        statement = args[2]
        if "concept_embeddings" in statement or "entity_embeddings" in statement:
            counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _incr)
    return counter


def test_matrix_cache_reused_within_ttl(session, monkeypatch):
    """Within VECTOR_MATRIX_TTL the corpus is not re-selected."""
    _seed_corpus(session)
    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "matrix")
    monkeypatch.setenv("VECTOR_MATRIX_TTL", "300")
    counter = _embedding_table_query_counter(session.get_bind())
    qv = _query_embedding("inflation")

    r1 = vector_backend.search_concept_candidates(session, qv, limit=3, model=MODEL_NAME)
    assert counter["n"] >= 1, "initial matrix load never queried the embeddings table"

    mark = counter["n"]
    r2 = vector_backend.search_concept_candidates(session, qv, limit=3, model=MODEL_NAME)
    assert counter["n"] == mark, "matrix was reloaded from the database within the TTL"
    assert [c["id"] for c in r1] == [c["id"] for c in r2]


def test_entity_matrix_cache_reused_within_ttl(session, monkeypatch):
    _seed_corpus(session)
    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "matrix")
    monkeypatch.setenv("VECTOR_MATRIX_TTL", "300")
    counter = _embedding_table_query_counter(session.get_bind())
    qv = _query_embedding("China")

    vector_backend.search_entity_candidates(session, qv, limit=3, model=MODEL_NAME)
    assert counter["n"] >= 1
    mark = counter["n"]
    vector_backend.search_entity_candidates(session, qv, limit=3, model=MODEL_NAME)
    assert counter["n"] == mark, "entity matrix was reloaded within the TTL"


def test_matrix_ttl_expiry_reloads(session, monkeypatch):
    """Expired matrix is reloaded on the next query."""
    _seed_corpus(session)
    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "matrix")
    monkeypatch.setenv("VECTOR_MATRIX_TTL", "0.05")
    counter = _embedding_table_query_counter(session.get_bind())
    qv = _query_embedding("inflation")

    vector_backend.search_concept_candidates(session, qv, limit=3, model=MODEL_NAME)
    assert counter["n"] >= 1
    time.sleep(0.1)

    vector_backend.search_concept_candidates(session, qv, limit=3, model=MODEL_NAME)
    assert counter["n"] >= 2, "expired matrix was not reloaded"


def test_engines_invalidate_searches_drops_matrix_cache(session, monkeypatch):
    """Write-tool invalidation beats the TTL."""
    _seed_corpus(session)
    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "matrix")
    monkeypatch.setenv("VECTOR_MATRIX_TTL", "300")
    counter = _embedding_table_query_counter(session.get_bind())
    qv = _query_embedding("inflation")

    vector_backend.search_concept_candidates(session, qv, limit=3, model=MODEL_NAME)
    mark = counter["n"]

    engines.invalidate_searches()
    vector_backend.search_concept_candidates(session, qv, limit=3, model=MODEL_NAME)
    assert counter["n"] > mark, "invalidate_searches() did not drop the matrix cache"


# ─── legacy entry points still behave on the default backend ─────────────────

def test_semantic_search_public_function_on_default_backend(session):
    _seed_corpus(session)
    from fd_open_data_mcp.semantic_search import semantic_search

    results = semantic_search("consumer price inflation", limit=5)
    assert results, "semantic_search found nothing"
    assert results[0]["code"] == "CPI_YOY"
    assert "CPI_SHADOW" not in {r["code"] for r in results}


def test_ai_search_semantic_layer_filters_unbound(session):
    _seed_corpus(session)
    from fd_open_data_mcp.ai_search import _semantic_search

    bound_only = _semantic_search("inflation", None, 10, include_unbound=False)
    assert bound_only and all(c["has_binding"] for c in bound_only)

    everything = _semantic_search("inflation", None, 10, include_unbound=True)
    assert len(everything) > len(bound_only), "include_unbound=True should add unbound concepts"


def test_entity_search_object_searches_via_backend(session):
    from fd_open_data_mcp import db as dbmod
    from fd_open_data_mcp.semantic.entity_search import EntitySemanticSearch

    _seed_corpus(session)
    search = EntitySemanticSearch(dbmod.get_database().database_url)
    try:
        results = search.search("China", limit=3)
        assert results and results[0]["code"] == "CN"
        assert results[0]["metadata"] == {"region": "asia"}
        typed = search.search("technology company", entity_type="stock", limit=3)
        assert typed and all(r["entity_type"] == "stock" for r in typed)
    finally:
        search.engine.dispose()


# ─── json vs pgvector equivalence (PG only) ──────────────────────────────────

_PG_DDL = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE TABLE sources (id serial PRIMARY KEY, name varchar(64) NOT NULL, label varchar(128) NOT NULL)",
    """
    CREATE TABLE functions (
        id serial PRIMARY KEY,
        source_id integer NOT NULL,
        command varchar(255) NOT NULL,
        verified boolean NOT NULL DEFAULT true,
        scanner_mode varchar(32) NOT NULL DEFAULT 'upstream-curated',
        bulk_history boolean NOT NULL DEFAULT false,
        bulk_snapshot boolean NOT NULL DEFAULT false
    )
    """,
    'CREATE TABLE "columns" (id serial PRIMARY KEY, function_id integer NOT NULL, name varchar(255) NOT NULL)',
    """
    CREATE TABLE concepts (
        id serial PRIMARY KEY,
        code varchar(128) NOT NULL,
        name_en varchar(255), name_zh varchar(255), category varchar(255),
        unit varchar(64), measure varchar(64),
        frequency varchar(32) NOT NULL DEFAULT 'unknown',
        entity_type varchar(32) NOT NULL, source varchar(64),
        verified boolean NOT NULL DEFAULT true,
        deprecated boolean NOT NULL DEFAULT false
    )
    """,
    """
    CREATE TABLE concept_embeddings (
        id serial PRIMARY KEY,
        concept_id integer NOT NULL,
        embedding jsonb,
        embedding_vec vector(384),
        model varchar(128) NOT NULL
    )
    """,
    """
    CREATE TABLE concept_bindings (
        id serial PRIMARY KEY,
        concept_id integer NOT NULL,
        column_id integer NOT NULL,
        confidence double precision NOT NULL DEFAULT 0,
        provenance varchar(32) NOT NULL DEFAULT 'llm',
        reviewed boolean NOT NULL DEFAULT false
    )
    """,
    """
    CREATE TABLE entities (
        id serial PRIMARY KEY,
        entity_type varchar(32) NOT NULL,
        code varchar(128) NOT NULL,
        name_en varchar(255), name_zh varchar(255),
        metadata_json jsonb
    )
    """,
    """
    CREATE TABLE entity_embeddings (
        id serial PRIMARY KEY,
        entity_id integer NOT NULL,
        embedding text,
        embedding_vec vector(384),
        model varchar(128) NOT NULL
    )
    """,
]


@pytest.fixture()
def pg_session():
    """Minimal pgvector schema in a scratch schema; dropped afterwards."""
    engine = create_engine(
        PG_DSN, connect_args={"options": "-csearch_path=fd_vec_eq_test"}
    )
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS fd_vec_eq_test CASCADE"))
        conn.execute(text("CREATE SCHEMA fd_vec_eq_test"))
    with engine.begin() as conn:
        for ddl in _PG_DDL:
            conn.execute(text(ddl))

    from sqlalchemy.orm import sessionmaker

    maker = sessionmaker(bind=engine)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS fd_vec_eq_test CASCADE"))
        engine.dispose()


def _backfill_vector_columns(session) -> None:
    """Mirror the JSON embeddings into embedding_vec (what migration 3.2 does)."""
    rows = session.execute(text(
        "SELECT id, embedding FROM concept_embeddings WHERE embedding_vec IS NULL"
    )).fetchall()
    for row in rows:
        vec = row.embedding if isinstance(row.embedding, str) else json.dumps(row.embedding)
        session.execute(
            text("UPDATE concept_embeddings SET embedding_vec = CAST(:v AS vector) WHERE id = :id"),
            {"v": str([float(x) for x in json.loads(vec)]), "id": row.id},
        )
    rows = session.execute(text(
        "SELECT id, embedding FROM entity_embeddings WHERE embedding_vec IS NULL"
    )).fetchall()
    for row in rows:
        session.execute(
            text("UPDATE entity_embeddings SET embedding_vec = CAST(:v AS vector) WHERE id = :id"),
            {"v": str([float(x) for x in json.loads(row.embedding)]), "id": row.id},
        )
    session.commit()


@pytest.mark.skipif(not PG_DSN, reason="FD_MCP_VECTOR_TEST_DSN not set — PG-only equivalence test skipped")
@pytest.mark.parametrize("query,entity_type,frequency,include_unbound", _CONCEPT_QUERIES)
def test_concept_results_equivalent_json_vs_pgvector(
    pg_session, monkeypatch, query, entity_type, frequency, include_unbound
):
    _seed_corpus(pg_session)
    _backfill_vector_columns(pg_session)
    qv = _query_embedding(query)

    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "json")
    expected = vector_backend.search_concept_candidates(
        pg_session, qv, entity_type=entity_type, limit=3,
        include_unbound=include_unbound, model=MODEL_NAME, frequency=frequency,
    )

    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "pgvector")
    actual = vector_backend.search_concept_candidates(
        pg_session, qv, entity_type=entity_type, limit=3,
        include_unbound=include_unbound, model=MODEL_NAME, frequency=frequency,
    )

    _assert_equivalent(expected, actual)
    if not include_unbound:
        assert all(c["has_binding"] for c in expected)


@pytest.mark.skipif(not PG_DSN, reason="FD_MCP_VECTOR_TEST_DSN not set — PG-only equivalence test skipped")
@pytest.mark.parametrize("query,entity_type", [
    ("China", None),
    ("technology company", "stock"),
])
def test_entity_results_equivalent_json_vs_pgvector(pg_session, monkeypatch, query, entity_type):
    _seed_corpus(pg_session)
    _backfill_vector_columns(pg_session)
    qv = _query_embedding(query)

    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "json")
    expected = vector_backend.search_entity_candidates(
        pg_session, qv, entity_type=entity_type, limit=3, model=MODEL_NAME,
    )

    monkeypatch.setenv("FD_MCP_VECTOR_BACKEND", "pgvector")
    actual = vector_backend.search_entity_candidates(
        pg_session, qv, entity_type=entity_type, limit=3, model=MODEL_NAME,
    )

    _assert_equivalent(expected, actual)
    assert expected
