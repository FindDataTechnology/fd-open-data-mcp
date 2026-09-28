"""mcp-search-engine-overhaul 4.1–4.3: registry corpus embedding + retrieval.

Spec semantic-search「语料覆盖」coverage over an isolated SQLite session:
the ``registry_entries`` / ``registry_indicator_embeddings`` tables are
created with raw SQL only (the production tables live on PostgreSQL; the
SQLite fixture omits the pgvector column). The real embedding model runs in
exactly one test; every other test installs a fake model so the corpus is
not re-encoded per test.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest
from sqlalchemy import text

from fd_open_data_mcp import engines, search_cache
from fd_open_data_mcp.embeddings.model import MODEL_NAME
from fd_open_data_mcp.semantic import registry_embeddings, registry_search
from fd_open_data_mcp.semantic.registry_search import search_registry

# SQLite stand-ins for the production DDL (registry subset the corpus path
# touches; embeddings without the pgvector column — SQLite has no vector type).
_DDL_STATEMENTS = (
    """
    CREATE TABLE registry_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_db TEXT, source_table TEXT, source_column TEXT,
        native_code TEXT, semantic_code TEXT,
        name_zh TEXT, name_en TEXT,
        unit TEXT, frequency TEXT, domain TEXT,
        verified INTEGER
    )
    """,
    """
    CREATE TABLE registry_indicator_embeddings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        registry_entry_id INTEGER NOT NULL UNIQUE
            REFERENCES registry_entries(id) ON DELETE CASCADE,
        embedding TEXT NOT NULL,
        model VARCHAR(128) NOT NULL,
        embedded_text TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (model, registry_entry_id)
    )
    """,
)


@pytest.fixture(autouse=True)
def _fresh_engines_and_cache(monkeypatch):
    """Clean singletons + enabled default-TTL result cache per test."""
    engines.reset_engines()
    search_cache.invalidate()
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("SEARCH_RESULT_CACHE_TTL", "300")
    yield
    engines.reset_engines()
    search_cache.invalidate()


class _FakeModel:
    """Constant-vector model: every text embeds to the same unit vector, so
    similarity is 1.0 everywhere — tests then assert exactly the filtering /
    dedup logic under test, not the geometry."""

    DIM = 4

    def encode(self, texts, **kwargs):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def get_embedding_dimension(self):
        return self.DIM

    def get_sentence_embedding_dimension(self):
        return self.DIM


@pytest.fixture
def fake_model(monkeypatch):
    fake = _FakeModel()
    monkeypatch.setattr(registry_embeddings, "get_model", lambda: fake)
    monkeypatch.setattr(registry_search, "get_model", lambda: fake)
    import fd_open_data_mcp.ai_search as ai_search_mod
    import fd_open_data_mcp.semantic_search as semantic_search_mod
    monkeypatch.setattr(semantic_search_mod, "_get_model", lambda: fake)
    monkeypatch.setattr(ai_search_mod, "_get_model", lambda: fake)
    return fake


def _create_tables(session):
    for ddl in _DDL_STATEMENTS:
        session.execute(text(ddl))
    session.commit()


def _add_entry(session, semantic_code, name_zh=None, name_en=None,
               source_db="china_yearbook", verified=1) -> int:
    row = session.execute(
        text(
            "INSERT INTO registry_entries "
            "(source_db, semantic_code, name_zh, name_en, verified) "
            "VALUES (:source_db, :semantic_code, :name_zh, :name_en, :verified)"
        ),
        {
            "source_db": source_db, "semantic_code": semantic_code,
            "name_zh": name_zh, "name_en": name_en, "verified": verified,
        },
    )
    session.commit()
    return session.execute(text("SELECT last_insert_rowid()")).scalar_one()


def _embedding_rows(session):
    return session.execute(
        text("SELECT registry_entry_id, model, embedded_text "
             "FROM registry_indicator_embeddings ORDER BY registry_entry_id")
    ).all()


_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts" / "embed_registry_indicators.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "embed_registry_indicators", _SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─── 场景一: verified registry-only indicator discoverable semantically ─────
# (the one test that pays for the real model)


def test_verified_registry_only_indicator_discoverable(session):
    """「grain output」 surfaces the verified yearbook indicator 粮食产量 with
    result_type=registry and its source_db — through the public tool path."""
    from fd_open_data_mcp.server import semantic_search as tool

    _create_tables(session)
    grain = _add_entry(session, "yr.grain_output", name_zh="粮食产量")
    gdp = _add_entry(session, "wb.gdp.current_usd",
                     name_zh="GDP（现价美元）", name_en="GDP (current US$)",
                     source_db="world_bank")
    power = _add_entry(session, "city.residential_electricity",
                       name_zh="居民生活用电量", source_db="china_city_panel")

    stats = registry_embeddings.reembed_entries(session, [grain, gdp, power])
    assert stats["embedded"] == 3 and stats["skipped_empty"] == 0

    result = tool("grain output", limit=5)
    assert result["cached"] is False
    assert result["count"] >= 1

    hits = [r for r in result["results"] if r.get("result_type") == "registry"]
    assert hits, "registry-only indicator must be visible on the public path"
    top = hits[0]
    assert top["semantic_code"] == "yr.grain_output"
    assert top["name_zh"] == "粮食产量"
    assert top["source_db"] == "china_yearbook"
    assert top["similarity"] > 0.3
    # the yearbook indicator outranks the unrelated verified distractors
    codes = [h["semantic_code"] for h in hits]
    assert codes.index("yr.grain_output") < codes.index("wb.gdp.current_usd")
    assert codes.index("yr.grain_output") < codes.index(
        "city.residential_electricity")

    # direct API shape parity
    direct = search_registry("grain output", limit=3, session=session)
    assert direct and direct[0]["semantic_code"] == "yr.grain_output"
    assert direct[0]["result_type"] == "registry"
    assert set(direct[0]) == {
        "semantic_code", "name_zh", "name_en",
        "source_db", "similarity", "result_type",
    }


# ─── 场景二: unverified entries stay invisible ──────────────────────────────


def test_unverified_entries_invisible_even_on_perfect_match(session, fake_model):
    """With a fake model every similarity is 1.0 — only the live verified
    JOIN can keep the unverified entry out of the results."""
    from fd_open_data_mcp.server import semantic_search as tool

    _create_tables(session)
    verified = _add_entry(session, "yr.grain_output", name_zh="粮食产量", verified=1)
    unverified = _add_entry(
        session, "yr.unverified_grain", name_zh="粮食产量（未核实）", verified=0)

    stats = registry_embeddings.reembed_entries(session, [verified, unverified])
    assert stats["embedded"] == 2  # both embedded — invisibility is query-time

    hits = search_registry("grain output", limit=10, session=session)
    codes = [h["semantic_code"] for h in hits]
    assert "yr.grain_output" in codes
    assert "yr.unverified_grain" not in codes

    result = tool("grain output", limit=10)
    all_codes = [r.get("code") or r.get("semantic_code")
                 for r in result["results"]]
    assert "yr.unverified_grain" not in all_codes


def test_missing_tables_fail_soft(session):
    """SQLite dev databases without the registry corpus: empty result, no raise."""
    assert search_registry("grain output", session=session) == []

    from fd_open_data_mcp.server import semantic_search as tool

    result = tool("anything", limit=5)
    assert result["count"] == 0 and result["results"] == []
    assert result["cached"] is False


# ─── 场景三: dedupe — local concept wins semantic_code collisions ───────────


def test_merge_registry_hits_local_wins_on_code_collision():
    local = [{"code": "yr.grain_output", "similarity": 0.9, "has_binding": True}]
    registry = [
        {"semantic_code": "yr.grain_output", "similarity": 0.95,
         "result_type": "registry"},
        {"semantic_code": "yr.other", "similarity": 0.5, "result_type": "registry"},
    ]
    merged = registry_search.merge_registry_hits(
        local, registry, limit=5,
        key=lambda c: (round(c["similarity"] / 0.05),
                       1 if c.get("has_binding") else 0, c["similarity"]),
    )
    assert [m["code"] if "code" in m else m["semantic_code"] for m in merged] == [
        "yr.grain_output", "yr.other",
    ], "colliding registry hit dropped, local kept; survivor appended"


def test_semantic_search_dedupes_registry_hit_behind_local_concept(
        session, fake_model):
    from fd_open_data_mcp.models import Concept, ConceptEmbedding
    from fd_open_data_mcp.server import semantic_search as tool

    _create_tables(session)
    collision = _add_entry(session, "yr.grain_output", name_zh="粮食产量")
    other = _add_entry(session, "yr.other_output", name_zh="其他产量")
    registry_embeddings.reembed_entries(session, [collision, other])

    session.add(Concept(code="yr.grain_output", entity_type="country",
                        frequency="yearly"))
    session.flush()
    # Raw list: the JSON column type serializes on write (storing a
    # pre-dumped string would double-encode).
    session.add(ConceptEmbedding(
        concept_id=session.query(Concept).filter_by(code="yr.grain_output").one().id,
        embedding=[1.0, 0.0, 0.0, 0.0], model=MODEL_NAME,
    ))
    session.commit()

    result = tool("grain output", limit=5)
    grain_rows = [r for r in result["results"]
                  if r.get("code") == "yr.grain_output"
                  or r.get("semantic_code") == "yr.grain_output"]
    assert len(grain_rows) == 1, "code collision must yield exactly one row"
    assert grain_rows[0].get("result_type") != "registry", "local concept wins"

    others = [r for r in result["results"]
              if r.get("semantic_code") == "yr.other_output"]
    assert others and others[0]["result_type"] == "registry", \
        "non-colliding registry hit stays visible"


# ─── unified / ai_search merge behavior ─────────────────────────────────────


class _StubEntitySearch:
    """Entity layer stub — the registry merge, not the entity model, is under
    test (the real engine gets its coverage in test_search_engine_overhaul)."""

    def search_unified(self, query, entity_type=None, limit=20):
        return [{"code": "CN", "name_en": "China",
                 "similarity": 0.9, "result_type": "entity"}]


def test_unified_and_ai_search_merge_registry_hits(session, fake_model, monkeypatch):
    from fd_open_data_mcp.server import ai_search as ai_tool
    from fd_open_data_mcp.server import semantic_search_unified as unified_tool

    monkeypatch.setattr(engines, "get_entity_search", lambda: _StubEntitySearch())

    _create_tables(session)
    entry = _add_entry(session, "yr.grain_output", name_zh="粮食产量")
    registry_embeddings.reembed_entries(session, [entry])

    unified = unified_tool("grain output", limit=5)
    assert unified["cached"] is False
    sims = [r["similarity"] for r in unified["results"]]
    assert sims == sorted(sims, reverse=True), "merged order is similarity desc"
    types = {r.get("result_type") for r in unified["results"]}
    assert {"entity", "registry"} <= types

    ai = ai_tool("grain output", limit=5)
    assert ai["cached"] is False
    concepts = ai["concepts"]
    assert concepts, "registry-only indicator rides the ai_search concepts list"
    assert all(c.get("result_type") == "registry" for c in concepts)
    assert concepts[-1]["semantic_code"] == "yr.grain_output", \
        "registry hits are appended at the concepts tail"


# ─── 场景四: embed job text construction + dry-run/resume logic ─────────────


def test_build_embed_text_skips_empty_segments():
    build = registry_embeddings.build_embed_text
    assert build("粮食产量", None, "yr.grain_output") == "粮食产量 | yr.grain_output"
    assert build(None, "Grain output", "yr.grain_output") == "Grain output | yr.grain_output"
    assert build("粮食产量", "", "yr.grain_output") == "粮食产量 | yr.grain_output"
    assert build("  粮食产量  ", " Grain output ", None) == "粮食产量 | Grain output"
    assert build(None, None, None) is None
    assert build("", "   ", "") is None


def test_reembed_entries_idempotent_and_counts(session, fake_model):
    _create_tables(session)
    plain = _add_entry(session, "yr.grain_output", name_zh="粮食产量")
    empty = _add_entry(session, None, name_zh=None, name_en=None)

    first = registry_embeddings.reembed_entries(session, [plain, empty, 999999])
    assert first == {"requested": 3, "embedded": 1,
                     "skipped_empty": 1, "missing": 1}
    second = registry_embeddings.reembed_entries(session, [plain])
    assert second["embedded"] == 1  # UPSERT, not a second row
    assert len(_embedding_rows(session)) == 1

    row = _embedding_rows(session)[0]
    assert row.model == MODEL_NAME
    assert row.embedded_text == "粮食产量 | yr.grain_output"


def test_embed_script_dry_run_counts_only(session, capsys):
    _create_tables(session)
    _add_entry(session, "yr.grain_output", name_zh="粮食产量")
    _add_entry(session, None)  # all three fields empty: counted, never embedded

    script = _load_script()
    assert script.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "1 pending" in out and "1 not embeddable" in out
    assert "Dry run" in out
    assert _embedding_rows(session) == [], "dry run must write nothing"


def test_embed_script_resume_by_rerun(session, fake_model, capsys):
    """Batch commits + LEFT JOIN pending set: rerun continues (and finishes)."""
    _create_tables(session)
    ids = [
        _add_entry(session, f"yr.ind{i:02d}", name_zh=f"指标{i}") for i in range(5)
    ]
    ids.append(_add_entry(session, None))  # all-empty: counted, skipped
    assert len(ids) == 6

    script = _load_script()
    assert script.main(["--batch-size", "2"]) == 0
    first = capsys.readouterr().out
    assert "embedded 5 rows this run" in first  # 5 text rows, batch-size 2
    assert "skipped 1 all-empty" in first
    assert "total embedded 5" in first
    assert "0 still pending" in first
    assert len(_embedding_rows(session)) == 5

    # rerun: nothing pending -> embeds 0, totals unchanged (resume = rerun)
    assert script.main(["--batch-size", "2"]) == 0
    second = capsys.readouterr().out
    assert "embedded 0 rows this run" in second
    assert "total embedded 5" in second
    assert len(_embedding_rows(session)) == 5

    # every embedded row carries the canonical corpus text
    for row in _embedding_rows(session):
        assert row.embedded_text.startswith("指标") and " | yr.ind" in row.embedded_text


def test_embed_script_requires_database_url(session, monkeypatch):
    monkeypatch.delenv("FD_OPEN_DATA_MCP_DATABASE_URL", raising=False)
    script = _load_script()
    with pytest.raises(SystemExit, match="FD_OPEN_DATA_MCP_DATABASE_URL"):
        script.main([])


def test_vector_column_absent_on_sqlite(session):
    _create_tables(session)
    assert registry_embeddings.vector_column_exists(session) is False
