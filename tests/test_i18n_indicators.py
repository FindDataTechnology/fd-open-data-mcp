"""Indicators board dictionary i18n smoke tests (panel-rbac-i18n-refresh 4.3).

Renders the indicator observatory board under the ``panel-lang=en`` cookie
and asserts the English dictionary surface (labels/headers/notices), plus
the zh default staying intact. Also pins the dropped-from-
test_panel_indicators English halves of the not-present notices (zh pages
are single-locale now) and guards that every ``t('…')`` key used by the
board's templates resolves in the merged dictionary.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from fd_open_data_mcp.models import Concept, ConceptFamily, ConceptMapping
from fd_open_data_mcp.panel import i18n
from fd_open_data_mcp.panel.app import app

client = TestClient(app)

TEMPLATES_DIR = Path(__file__).parent.parent / "fd_open_data_mcp" / "panel" / "templates"
BOARD_TEMPLATES = (
    "indicators.html", "indicator_families.html", "indicator_family.html",
    "indicator_detail.html", "indicator_relations.html",
    "indicator_coverage.html", "indicator_graph.html",
    "indicator_scopes.html", "indicator_scope_detail.html",
    "partial_indicator_relations.html", "partial_indicator_results.html",
)

# Same registry shape as tests/test_panel_indicators.py (raw-SQL registry).
REGISTRY_DDL = """
CREATE TABLE registry_entries (
  id INTEGER PRIMARY KEY,
  source_db TEXT, source_table TEXT, source_column TEXT,
  native_code TEXT, semantic_code TEXT,
  name_zh TEXT, name_en TEXT, unit TEXT, frequency TEXT,
  domain TEXT, verified BOOLEAN
)"""


def _make_registry(session, rows):
    session.execute(text(REGISTRY_DDL))
    for i, r in enumerate(rows, start=1):
        session.execute(text(
            "INSERT INTO registry_entries (id, source_db, source_table, source_column,"
            " native_code, semantic_code, name_zh, name_en, unit, frequency, domain, verified)"
            " VALUES (:id,:source_db,:source_table,:source_column,:native_code,:semantic_code,"
            ":name_zh,:name_en,:unit,:frequency,:domain,:verified)"),
            {"id": i, **r})
    session.commit()


def _reg(semantic_code, source_db, native_code, domain, verified, name_zh):
    return {"source_db": source_db, "source_table": "tbl", "source_column": "col",
            "native_code": native_code, "semantic_code": semantic_code,
            "name_zh": name_zh, "name_en": f"en:{native_code}", "unit": None,
            "frequency": "daily", "domain": domain, "verified": verified}


def _seed(session):
    """Registry rows + one family/concept/mapping so every board tab has data."""
    _make_registry(session, [
        _reg("fd:GDP", "db-wind", "W-GDP-1", "macro", True, "国内生产总值"),
        _reg("fd:GDP", "db-akshare", "A-GDP-1", "macro", True, "GDP"),
        _reg("fd:POP", "db-wind", "W-POP-1", "macro", False, "人口总数"),
    ])
    fam = ConceptFamily(code="fd:GDP", name_zh="GDP", name_en="GDP")
    session.add(fam)
    session.flush()
    c = Concept(code="fd:GDP.nominal", concept_code="fd:GDP", name_zh="名义GDP",
                name_en="Nominal GDP", entity_type="country",
                frequency="quarterly")
    session.add(c)
    session.flush()
    session.add(ConceptMapping(concept_id=c.id, vocabulary="wikidata",
                               term="Q11111", relation="exact",
                               confidence=0.9, provenance="manual"))
    session.commit()
    return c


def _en(path, **kwargs):
    return client.get(path, cookies={"panel-lang": "en"}, **kwargs)


def _norm(html):
    return re.sub(r"\s+", " ", html)


# ── dictionary hygiene ───────────────────────────────────────────────────────

def test_group_file_loaded_and_every_template_key_resolves():
    """i18n.py merged i18n_en_indicators.EN_ENTRIES, and every ``t('…')`` key
    the board's templates use resolves to a translation (no silent zh
    fallback from a typo'd key)."""
    from fd_open_data_mcp.panel.i18n_en_indicators import EN_ENTRIES

    assert EN_ENTRIES  # non-empty group file
    assert i18n.EN.get("语义代码") == "Semantic code"  # merged into EN

    used: set[str] = set()
    for name in BOARD_TEMPLATES:
        used |= set(re.findall(r"\bt\('([^']+)'\)",
                               (TEMPLATES_DIR / name).read_text(encoding="utf-8")))
    assert used, "template key extraction found nothing — regex drifted"
    missing = sorted(k for k in used if k not in i18n.EN)
    assert not missing, f"t() keys missing from the dictionary: {missing}"


def test_group_entries_are_wellformed():
    from fd_open_data_mcp.panel.i18n_en_indicators import EN_ENTRIES
    for k, v in EN_ENTRIES.items():
        assert isinstance(k, str) and k.strip(), repr(k)
        assert isinstance(v, str), repr((k, v))


# ── en render smoke: every tab of the board ─────────────────────────────────

def test_registry_board_en(session):
    c = _seed(session)
    r = _en("/panel/indicators")
    assert r.status_code == 200
    t = _norm(r.text)
    # header / filter / paging envelope in English
    assert "Semantic code" in t and "Native code" in t and "Source db" in t
    assert "3 entries" in t and "Page 1/1" in t
    assert "all domains" in t and "all sources" in t
    # the unverified badge's tooltip carries the en dictionary entry
    assert 'title="eligible for the public catalog only once verified"' in r.text
    # concept link target still works and detail renders in English
    d = _en(f"/panel/indicators/concepts/{c.id}")
    assert d.status_code == 200
    dt = _norm(d.text)
    assert "Cross-source equivalence (registry anchors)" in dt
    assert "Local bindings" in dt and "Vocabulary mappings" in dt
    assert "back to registry" in dt


def test_families_relations_coverage_graph_scopes_en(session):
    _seed(session)
    fam = _norm(_en("/panel/indicators/families").text)
    assert "Concept families" in fam and "Family code" in fam
    assert "Member concepts" in fam and "Bindings" in fam

    fam_detail = _norm(_en("/panel/indicators/families/fd:GDP").text)
    assert "Member concepts (1)" in fam_detail and "Entity" in fam_detail
    assert "back to families" in fam_detail

    rel = _norm(_en("/panel/indicators/relations").text)
    assert "Vocabulary mappings (1)" in rel
    assert "all vocabularies" in rel and "all relations" in rel
    assert "Cross-source bindings" in rel and "Provenance" in rel

    cov = _norm(_en("/panel/indicators/coverage").text)
    assert "By source database" in cov and "By domain" in cov
    assert "Registered" in cov and "Verified %" in cov
    assert "Registered and verified by source database" in cov  # chart aria

    graph = _norm(_en("/panel/indicators/graph").text)
    assert "Interactive relation graph" in graph
    assert "Node detail" in graph and "Select a node to see its detail." in graph
    assert "Relation listing (no-script fallback)" in graph
    assert "Latest vocabulary mappings" in graph

    scopes = _norm(_en("/panel/indicators/scopes").text)
    assert "Retrieval scopes" in scopes and "Hits (7d)" in scopes
    assert "No scopes." in scopes and "scope name" in scopes


def test_scope_detail_en_roundtrip(session):
    _seed(session)
    r = client.post("/panel/indicators/scopes/create", data={
        "name": "wb-only", "description": "world_bank only",
        "source_dbs": "db-wind", "domains": "", "semantic_codes": "",
        "native_codes": ""})
    assert r.status_code == 200
    d = _norm(_en("/panel/indicators/scopes/wb-only").text)
    assert "Hit statistics" in d and "Edit rules" in d
    assert "Delete this scope" in d and "back to scopes" in d


# ── en render of the not-present notices (moved from zh-page assertions) ─────

def test_missing_registry_notices_en(session):
    """No registry_entries → the en locale carries the English halves that
    used to ride inline on the zh page (now single-locale per locale)."""
    r = _en("/panel/indicators")
    assert r.status_code == 200
    t = _norm(r.text)
    assert "not present" in t and "the board only observes" in t

    r = _en("/panel/indicators/coverage")
    assert r.status_code == 200
    t = _norm(r.text)
    assert "not present" in t and "counts are unavailable" in t


# ── zh default unchanged ─────────────────────────────────────────────────────

def test_zh_default_labels_unchanged(session):
    _seed(session)
    r = client.get("/panel/indicators")
    assert r.status_code == 200
    t = _norm(r.text)
    assert "语义代码" in t and "原生代码" in t
    assert "3 条 entries" in t and "第 1/1 页 page" in t  # envelope bytes kept
    assert "Semantic code" not in t  # no en bleed on the zh page
    assert "全部域" in t and "筛选" in t

    fam = _norm(client.get("/panel/indicators/families").text)
    assert "概念族谱" in fam and "族代码" in fam and "绑定数" in fam

    scopes = _norm(client.get("/panel/indicators/scopes").text)
    assert "检索范围" in scopes and "暂无 scope" in scopes

    cov = _norm(client.get("/panel/indicators/coverage").text)
    assert "按源库" in cov and "验证率" in cov
