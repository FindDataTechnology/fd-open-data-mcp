"""mcp-search-engine-overhaul: portable model resolution, registration
uniqueness, engine singletons with TTL caches, result-cache behavior, and
SQLite-portable value SQL — one test file per spec scenario."""
from __future__ import annotations

import asyncio
import ast
import json
import os
import pathlib
import time

import pytest
from sqlalchemy import event

import fd_open_data_mcp
from fd_open_data_mcp import engines, search_cache


@pytest.fixture(autouse=True)
def _fresh_engines_and_cache(monkeypatch):
    """Each test gets clean singletons + an enabled default-TTL result cache."""
    engines.reset_engines()
    search_cache.invalidate()
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("SEARCH_RESULT_CACHE_TTL", "300")
    monkeypatch.setenv("GRAPH_CACHE_TTL", "300")
    yield
    engines.reset_engines()
    search_cache.invalidate()


def _pkg_dir() -> pathlib.Path:
    return pathlib.Path(fd_open_data_mcp.__file__).parent


# ─── 1.1 Portable model resolution ──────────────────────────────────────────

SEARCH_MODULES = [
    "semantic_search.py",
    "ai_search.py",
    "embeddings/model.py",
    "semantic/entity_search.py",
]


@pytest.mark.parametrize("rel", SEARCH_MODULES)
def test_no_machine_specific_paths_in_search_modules(rel):
    src = (_pkg_dir() / rel).read_text()
    assert "/Users/" not in src, f"{rel} still references a machine-specific path"
    assert "MODEL_PATH" not in src


def test_model_resolves_by_name():
    from fd_open_data_mcp.embeddings.model import MODEL_NAME, get_model

    assert MODEL_NAME == "all-MiniLM-L6-v2"
    model = get_model()
    emb = model.encode(["inflation"])[0]
    assert len(emb) == 384  # all-MiniLM-L6-v2 dimension


def test_model_load_failure_names_the_model(monkeypatch):
    import fd_open_data_mcp.embeddings.model as mod

    monkeypatch.setattr(mod, "MODEL_NAME", "no-such-model-xyz")
    mod.reset_model()
    try:
        with pytest.raises(RuntimeError, match="no-such-model-xyz"):
            mod.get_model()
    finally:
        mod.reset_model()


# ─── 1.2 Registration uniqueness ────────────────────────────────────────────

def test_no_module_level_tool_registration_outside_server():
    """server.py is the only registration point; impl modules export plain
    functions (spec: each tool registered exactly once)."""
    violators = []
    for f in _pkg_dir().rglob("*.py"):
        if f.name == "server.py" or "__pycache__" in f.parts:
            continue
        tree = ast.parse(f.read_text())
        for node in tree.body:  # module level only
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if ast.unparse(dec).startswith("mcp.tool"):
                        violators.append(f"{f.relative_to(_pkg_dir())}:{node.name}")
    assert violators == [], f"module-level @mcp.tool found: {violators}"


def _tool_snapshot():
    from fd_open_data_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names registered"
    return {
        t.name: (t.description, json.dumps(getattr(t, "parameters", {}), sort_keys=True, default=str))
        for t in tools
    }


def test_tool_definitions_identical_before_and_after_call(session):
    """Calling a tool (whose lazy import used to re-register module-level
    duplicates) must not change any tool definition (spec scenario)."""
    before = _tool_snapshot()

    from fd_open_data_mcp.server import get_entity

    assert get_entity("country", "CN") is None  # exercises entity_graph_tools import
    import fd_open_data_mcp.ai_search  # noqa: F401 — import side effects under test
    import fd_open_data_mcp.semantic_search  # noqa: F401
    import fd_open_data_mcp.entity_graph_tools  # noqa: F401

    after = _tool_snapshot()
    assert before == after


# ─── 1.3 Engine singletons + TTL wiring ─────────────────────────────────────

def _seed_entities(session, n=3):
    from fd_open_data_mcp.models import Entity

    for i in range(n):
        session.add(Entity(entity_type="country", code=f"C{i}", name_en=f"Country {i}"))
    session.commit()


def _sql_counter(mgr):
    counter = {"n": 0}
    event.listen(mgr.engine, "before_cursor_execute", lambda *a, **k: counter.__setitem__("n", counter["n"] + 1))
    return counter


def test_graph_manager_is_singleton(session):
    _seed_entities(session)
    assert engines.get_graph_manager() is engines.get_graph_manager()


def test_graph_reused_within_ttl_without_db_reload(session):
    """Second call within GRAPH_CACHE_TTL must not reload from the database
    (spec scenario: graph reuse within TTL)."""
    _seed_entities(session)
    mgr = engines.get_graph_manager()
    g1 = mgr.get_graph()
    assert g1.number_of_nodes() == 3

    counter = _sql_counter(mgr)
    g2 = mgr.get_graph()
    assert g2 is g1
    assert counter["n"] == 0, "graph was reloaded from the database within the TTL"


def test_graph_cache_ttl_env_wired(session, monkeypatch):
    """GRAPH_CACHE_TTL=0 expires the cache immediately -> reload each call."""
    _seed_entities(session)
    monkeypatch.setenv("GRAPH_CACHE_TTL", "0")
    engines.reset_engines()
    mgr = engines.get_graph_manager()
    mgr.get_graph()
    counter = _sql_counter(mgr)
    mgr.get_graph()
    assert counter["n"] > 0, "TTL=0 should force a database reload"


def test_graph_write_invalidation(session):
    """Entity writes drop the cached graph immediately (no TTL wait)."""
    _seed_entities(session)
    mgr = engines.get_graph_manager()
    assert mgr.get_graph().number_of_nodes() == 3
    counter = _sql_counter(mgr)

    engines.invalidate_graph()
    assert mgr.get_graph().number_of_nodes() == 3
    assert counter["n"] > 0, "invalidation should force a reload on next access"


def test_entity_search_is_singleton_and_cache_survives(session):
    """EntitySemanticSearch is a process singleton; its embedding query cache
    survives across calls (EMBEDDING_CACHE_SIZE wired)."""
    from fd_open_data_mcp.models import Entity, EntityEmbedding

    session.add(Entity(entity_type="country", code="CN", name_en="China"))
    session.flush()
    emb = engines.get_entity_search().model.encode("China").tolist()
    session.add(EntityEmbedding(entity_id=1, embedding=json.dumps(emb), model="all-MiniLM-L6-v2"))
    session.commit()

    assert engines.get_entity_search() is engines.get_entity_search()
    s = engines.get_entity_search()
    s.invalidate_cache()
    s._get_embedding("china query")
    assert s.get_cache_stats()["cache_misses"] == 1
    s._get_embedding("china query")
    assert s.get_cache_stats()["cache_hits"] == 1


def test_embedding_cache_size_env_wired(monkeypatch):
    from fd_open_data_mcp.semantic.entity_search import EntitySemanticSearch

    monkeypatch.setenv("EMBEDDING_CACHE_SIZE", "10")
    s = EntitySemanticSearch("sqlite:///:memory:")
    try:
        for i in range(30):
            s._get_embedding(f"distinct query {i}")
        # 12 warm-up entries + a bounded working set: never the raw 42 inserts
        assert len(s._embedding_cache) <= 13
    finally:
        s.engine.dispose()


# ─── 1.4 Delegated retrieval benefits equally ───────────────────────────────

_BIZ_SERVER = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fd-find-data-business-mcp"
    / "fd_find_data_business_mcp"
    / "server.py"
)


@pytest.mark.skipif(not _BIZ_SERVER.exists(), reason="business-mcp repo not checked out alongside")
def test_business_delegation_uses_shared_singleton():
    """business-mcp graph_search delegates through the shared engines singleton
    (spec: delegated retrieval benefits equally). Runtime behavior is covered
    by the business repo's own test suite."""
    src = _BIZ_SERVER.read_text()
    assert "engines.get_graph_manager()" in src
    assert "EntityGraphManager(db.database_url)" not in src


# ─── 2.1 Result TTL cache ───────────────────────────────────────────────────

def test_result_cache_hit_marks_cached(session):
    from fd_open_data_mcp.server import graph_search

    r1 = graph_search("statistics", "")
    assert r1["cached"] is False
    r2 = graph_search("statistics", "")
    assert r2["cached"] is True, "second identical call should be a cache hit"
    assert r2["node_count"] == r1["node_count"]


def test_result_cache_expiry(session, monkeypatch):
    from fd_open_data_mcp.server import graph_search

    monkeypatch.setenv("SEARCH_RESULT_CACHE_TTL", "0.05")
    r1 = graph_search("statistics", "")
    time.sleep(0.1)
    r2 = graph_search("statistics", "")
    assert r2["cached"] is False, "expired entry must be recomputed"


def test_result_cache_invalidated_by_write(session):
    from fd_open_data_mcp.server import add_entity, graph_search

    graph_search("statistics", "")  # warm the cache
    add_entity(entity_type="country", code="ZZ", name_en="Zedland")
    r = graph_search("statistics", "")
    assert r["cached"] is False, "entity write must invalidate cached results"
    assert r["node_count"] == 1


def test_result_cache_disabled(session, monkeypatch):
    from fd_open_data_mcp.server import graph_search

    monkeypatch.setenv("CACHE_ENABLED", "false")
    # Disabled cache passes the raw result through: no marker, no caching.
    assert "cached" not in graph_search("statistics", "")
    assert "cached" not in graph_search("statistics", "")


def test_error_results_not_cached(session):
    from fd_open_data_mcp.server import graph_search

    r1 = graph_search("statistics", "")
    del r1  # ensure store has a good entry; now an error path
    r2 = graph_search("bogus_algorithm", "CN")
    assert "error" in r2 and "cached" not in r2
    r3 = graph_search("bogus_algorithm", "CN")
    assert "error" in r3 and "cached" not in r3


# ─── 2.2 SQLite-portable value SQL ──────────────────────────────────────────

def _seed_observations(session):
    from fd_open_data_mcp.models import Concept, SemanticObservation

    c = Concept(code="CPI_YOY", entity_type="country", frequency="monthly")
    session.add(c)
    session.flush()
    for date, value in (("2024-01-01", "1.1"), ("2024-02-01", "2.2"), ("2024-03-01", "3.3")):
        session.add(SemanticObservation(
            concept_id=c.id, entity_type="country", entity_id=1,
            date=date, value=value, unit="%", source_used="test",
        ))
    session.commit()
    return c


def test_fetch_values_latest_per_pair_on_sqlite(session):
    from fd_open_data_mcp.ai_search import _fetch_values

    c = _seed_observations(session)
    values = _fetch_values([{"id": c.id}], "country", None)
    assert len(values) == 1
    assert values[0]["date"] == "2024-03-01"  # latest per (concept, entity)


def test_fetch_values_exact_date_on_sqlite(session):
    from fd_open_data_mcp.ai_search import _fetch_values

    c = _seed_observations(session)
    values = _fetch_values([{"id": c.id}], "country", "2024-01-01")
    assert len(values) == 1
    assert values[0]["date"] == "2024-01-01" and values[0]["value"] == "1.1"


def test_ai_search_end_to_end_on_sqlite(session):
    """Full three-layer orchestration on a SQLite dev database (spec scenario:
    ai_search on SQLite completes without a dialect error)."""
    from fd_open_data_mcp.ai_search import ai_search as _ai
    from fd_open_data_mcp.models import ConceptEmbedding, Entity

    c = _seed_observations(session)
    session.add(Entity(entity_type="country", code="CN", name_en="China"))
    emb = engines.get_entity_search().model.encode("consumer price inflation").tolist()
    # Store the raw list: the JSONB column type serializes on write (the
    # production PG path stores json.dumps text; both parse back to a list).
    session.add(ConceptEmbedding(concept_id=c.id, embedding=emb, model="all-MiniLM-L6-v2"))
    session.commit()

    result = _ai("consumer price inflation", entity_type="country",
                 limit=5, include_values=True, include_unbound=True)
    assert result["concepts"], "concept layer found nothing"
    assert result["entities"], "entity layer found nothing"
    assert result["values"] and result["values"][0]["date"] == "2024-03-01"
