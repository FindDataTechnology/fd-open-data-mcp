"""Indicator observatory board tests (panel-indicator-observatory).

Pins the board's spec scenarios against the SSR panel: 门禁 gate parity +
导航 nav entry (crawl-control-center), 过滤 registry filtering with
unverified badges + paging envelope, 数据同源 coverage agreement with the
same GROUP BY SQL run directly, 族谱 family views with binding counts,
等价对 cross-source equivalence pairs, 映射可检索 searchable mappings,
bounded relation graph views + node cap, panel-ui 自托管 self-hosted static
assets, 降级/无脚本 noscript fallback, 节点详情 node-detail JSON contract,
missing-registry degradation, and the scope-管理面 wave boundary (absent).
"""
from __future__ import annotations

import re
from importlib import reload

from fastapi.testclient import TestClient
from sqlalchemy import text

from fd_open_data_mcp.models import (
    Concept, ConceptBinding, ConceptFamily, ConceptMapping, Function,
    FunctionColumn, Source,
)
from fd_open_data_mcp.panel import observatory
from fd_open_data_mcp.panel.app import app

client = TestClient(app)

# The registry is read by the panel through raw SQL (fail-soft), not the ORM,
# so tests create it explicitly in the fixture's SQLite DB. DDL matches the
# production PG columns the code selects.
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


def _seed_column(session, source_name, command, column_name) -> FunctionColumn:
    """Source -> Function -> FunctionColumn chain for a ConceptBinding.
    Reuses the Source/Function rows (both have unique constraints)."""
    src = session.query(Source).filter_by(name=source_name).first()
    if src is None:
        src = Source(name=source_name, label=f"{source_name} label")
        session.add(src)
        session.flush()
    fn = (session.query(Function)
          .filter_by(source_id=src.id, command=command).first())
    if fn is None:
        fn = Function(source_id=src.id, command=command)
        session.add(fn)
        session.flush()
    col = FunctionColumn(function_id=fn.id, name=column_name)
    session.add(col)
    session.flush()
    return col


def _seed_concept(session, code, family_code=None, name_zh="指标", name_en="Indicator",
                  entity_type="country", frequency="quarterly") -> Concept:
    c = Concept(code=code, concept_code=family_code, name_zh=name_zh,
                name_en=name_en, entity_type=entity_type, frequency=frequency)
    session.add(c)
    session.flush()
    return c


def _norm(html):
    """Collapse template line-wrapping so phrase assertions match the text a
    browser renders (HTML collapses whitespace)."""
    return re.sub(r"\s+", " ", html)


def _tr_blocks(html):
    return re.findall(r"<tr>(.*?)</tr>", html, re.S)


def _cells(block):
    return [re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", block, re.S)]


def _tr_cells(html):
    return [cells for cells in map(_cells, _tr_blocks(html)) if cells]


# 12 rows: 2 domains x 2 source_dbs, verified True/False mixed.
# (semantic_code, source_db, native_code, domain, verified, name_zh)
_FILTER_ROWS = [
    ("fd:GDP", "db-wind", "W-GDP-1", "macro", True, "国内生产总值"),
    ("fd:CPI", "db-wind", "W-CPI-1", "macro", True, "居民消费价格指数"),
    ("fd:POP", "db-wind", "W-POP-1", "macro", False, "人口总数"),
    ("fd:UNEMP", "db-akshare", "A-UNE-1", "macro", True, "失业率"),
    ("fd:PMI", "db-akshare", "A-PMI-1", "macro", False, "采购经理指数"),
    ("fd:FDI", "db-akshare", "A-FDI-1", "macro", True, "外商直接投资"),
    ("fd:M2", "db-akshare", "A-M2-1", "finance", True, "广义货币"),
    ("fd:SI", "db-akshare", "A-SI-1", "finance", False, "社会融资"),
    ("fd:LEND", "db-wind", "W-LEN-1", "finance", True, "贷款余额"),
    ("fd:FOREX", "db-wind", "W-FX-1", "finance", False, "外汇储备"),
    ("fd:BOND", "db-wind", "W-BD-1", "finance", True, "债券余额"),
    ("fd:BAL", "db-akshare", "A-BAL-1", "finance", False, "财政收支"),
]


# ── scenario 1: 门禁 gate + 导航 nav (crawl-control-center) ──────────────────

def test_board_pages_render_and_nav_entry(session):
    """indicator-observatory 门禁/导航: every board page renders 200 and every
    panel page's base shell carries the 指标 Indicators nav entry."""
    fam = ConceptFamily(code="fd:GDP", name_zh="GDP", name_en="GDP")
    session.add(fam)
    session.flush()
    c = _seed_concept(session, "fd:GDP.nominal", family_code="fd:GDP")
    session.commit()

    paths = [
        "/panel/indicators",
        "/panel/indicators/families",
        "/panel/indicators/families/fd:GDP",
        f"/panel/indicators/concepts/{c.id}",
        "/panel/indicators/relations",
        "/panel/indicators/coverage",
        "/panel/indicators/graph",
        f"/panel/indicators/graph.json?indicator={c.id}",
    ]
    for p in paths:
        assert client.get(p).status_code == 200, p

    # the nav entry is in the shared base shell of other boards AND this one
    for p in ("/panel/policies", "/panel/runs", "/panel/indicators/families"):
        html = client.get(p).text
        assert 'href="/panel/indicators"' in html, p
        assert '指标 <span class="en">Indicators</span></a>' in html, p


def test_gate_parity_with_other_boards(session, monkeypatch):
    """门禁: PANEL_TOKEN gates the new board exactly like the other boards
    (401 without a token, 200 with the header token)."""
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    import fd_open_data_mcp.panel.app as appmod
    gated = TestClient(reload(appmod).app)
    assert gated.get("/panel/policies").status_code == 401
    assert gated.get("/panel/indicators").status_code == 401
    assert gated.get("/panel/indicators",
                     headers={"X-Panel-Token": "sekret"}).status_code == 200
    # rebuild the module-level app with the env reverted so later test files
    # that import the app at runtime do not inherit this gated instance
    monkeypatch.undo()
    reload(appmod)


# ── scenario 2: 过滤 filtering + unverified 徽标 + paging envelope ────────────

def test_registry_filter_badge_and_keyword(session):
    """过滤: domain+verified filters narrow the table; unverified entries stay
    visible carrying the `badge unverified` markup, verified ones `badge
    success`; q matches name_zh or semantic_code; htmx gets the partial."""
    _make_registry(session, [_reg(*r) for r in _FILTER_ROWS])

    r = client.get("/panel/indicators")
    assert r.status_code == 200
    # paging envelope (total/pages) renders
    assert "12 条 entries" in r.text and "1/1" in r.text
    # one badge per row; both calibers visible on the unfiltered listing
    assert r.text.count('class="badge') == 12
    assert r.text.count('class="badge success"') == 7
    assert r.text.count('class="badge unverified') == 5

    # domain + verified=1 => only matching rows appear
    r = client.get("/panel/indicators",
                   params={"domain": "macro", "verified": "1"})
    assert r.status_code == 200
    for code in ("fd:GDP", "fd:CPI", "fd:UNEMP", "fd:FDI"):
        assert code in r.text
    for code in ("fd:POP", "fd:PMI", "fd:M2", "fd:SI", "fd:LEND",
                 "fd:FOREX", "fd:BOND", "fd:BAL"):
        assert code not in r.text
    assert r.text.count('class="badge') == 4

    # keyword over semantic_code
    r = client.get("/panel/indicators", params={"q": "fd:G"})
    assert "fd:GDP" in r.text and "fd:CPI" not in r.text
    # keyword over name_zh
    r = client.get("/panel/indicators", params={"q": "生产总值"})
    assert "fd:GDP" in r.text and "fd:M2" not in r.text

    # htmx request swaps in the results partial, not the full page
    r = client.get("/panel/indicators", params={"domain": "macro"},
                   headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text
    assert "fd:GDP" in r.text and "fd:M2" not in r.text


def test_registry_pagination(session):
    """过滤/分页: per_page 50 server-side; page 2 carries the remainder and
    the 下一页 next / 上一页 prev links exist."""
    rows = [_reg(f"bulk:{i:03d}", "db-bulk", f"N{i:03d}", "bulk",
                 i % 2 == 0, f"批量指标{i}") for i in range(60)]
    _make_registry(session, rows)

    p1 = client.get("/panel/indicators")
    assert "60 条 entries" in p1.text and "1/2" in p1.text
    assert p1.text.count('class="badge') <= 50
    assert p1.text.count('class="badge') == 50
    assert "下一页" in p1.text and "&page=2" in p1.text

    p2 = client.get("/panel/indicators", params={"page": 2})
    assert p2.text.count('class="badge') == 10
    assert "上一页" in p2.text
    assert "bulk:059" in p2.text and "bulk:059" not in p1.text


# ── scenario 3: 数据同源 — coverage equals the same SQL run directly ─────────

def test_coverage_agrees_with_same_sql(session):
    """数据同源 (crawl-control-center): the coverage table cells equal what the
    same registry_coverage GROUP BY returns against the same DB right now —
    the board observes, never forks."""
    _make_registry(session, [
        _reg("fd:G1", "db-wind", "W1", "macro", True, "甲"),
        _reg("fd:G2", "db-wind", "W2", "macro", True, "乙"),
        _reg("fd:G3", "db-wind", "W3", "macro", True, "丙"),
        _reg("fd:G4", "db-wind", "W4", "macro", False, "丁"),
        _reg("fd:G5", "db-wind", "W5", "macro", None, "戊"),  # NULL verified
        _reg("fd:G6", "db-akshare", "A1", "macro", True, "己"),
        _reg("fd:G7", "db-akshare", "A2", "finance", True, "庚"),
        _reg("fd:G8", "db-akshare", "A3", "finance", False, "辛"),
        _reg("fd:G9", "db-akshare", "A4", "finance", False, "壬"),
    ])
    expected = {}
    res = session.execute(text(
        "SELECT source_db, count(*), count(*) FILTER (WHERE verified), "
        "count(*) FILTER (WHERE verified IS NOT TRUE) "
        "FROM registry_entries GROUP BY source_db"))
    for source_db, registered, verified, unverified in res:
        expected[source_db] = (int(registered), int(verified), int(unverified))
    assert expected == {"db-wind": (5, 3, 2), "db-akshare": (4, 2, 2)}

    r = client.get("/panel/indicators/coverage")
    assert r.status_code == 200
    got = {}
    for cells in _tr_cells(r.text):
        if cells and cells[0] in expected:
            got[cells[0]] = tuple(int(x) for x in cells[1:4])
    assert got == expected
    # and each source_db's three numbers literally appear in the page text
    for source_db, nums in expected.items():
        assert source_db in r.text
        for n in nums:
            assert str(n) in r.text


# ── scenario 4: 族谱 — families list + member binding counts ─────────────────

def test_families_list_and_detail_binding_counts(session):
    """族谱: the families list shows member counts and summed bindings; the
    family detail lists each member with its own binding_count."""
    fam = ConceptFamily(code="fd:GDP", name_zh="GDP", name_en="GDP",
                        description="output family")
    session.add(fam)
    session.flush()
    c1 = _seed_concept(session, "fd:GDP.nominal", "fd:GDP", name_zh="名义GDP")
    c2 = _seed_concept(session, "fd:GDP.ppp", "fd:GDP", name_zh="购买力平价GDP")
    col1 = _seed_column(session, "akshare", "gdp_hist", "gdp_nominal_col_a")
    col2 = _seed_column(session, "wind", "gdp_series", "gdp_nominal_col_w")
    col3 = _seed_column(session, "wind", "gdp_series", "gdp_ppp_col_w")
    session.add_all([
        ConceptBinding(concept_id=c1.id, column_id=col1.id, confidence=0.9,
                       provenance="manual", reviewed=True),
        ConceptBinding(concept_id=c1.id, column_id=col2.id, confidence=0.8,
                       provenance="llm"),
        ConceptBinding(concept_id=c2.id, column_id=col3.id, confidence=0.7,
                       provenance="llm"),
    ])
    session.commit()

    r = client.get("/panel/indicators/families")
    assert r.status_code == 200
    row = next(c for c in _tr_cells(r.text) if c and c[0] == "fd:GDP")
    assert row[2] == "2"   # member concepts
    assert row[3] == "3"   # summed bindings

    r = client.get("/panel/indicators/families/fd:GDP")
    assert r.status_code == 200
    for cid, count in ((c1.id, "2"), (c2.id, "1")):
        block = next(b for b in _tr_blocks(r.text)
                     if f"/panel/indicators/concepts/{cid}\"" in b)
        cells = _cells(block)
        assert cells[4] == count   # binding_count sits in that concept's row


# ── scenario 5: 等价对 — registry anchors on the concept page ─────────────────

def test_concept_detail_equivalence_pairs(session):
    """等价对: a Concept whose code equals a semantic_code lists every registry
    anchor's native code (the cross-source equivalence set) plus its local
    ConceptBinding column names."""
    _make_registry(session, [
        _reg("fd:CPI", "db-wind", "W-CPI-1", "macro", True, "居民消费价格指数"),
        _reg("fd:CPI", "db-akshare", "A-CPI-9", "macro", False, "CPI年率"),
        _reg("fd:CPI", "db-akshare", "A-CPI-10", "macro", True, "CPI月率"),
    ])
    c = _seed_concept(session, "fd:CPI", name_zh="居民消费价格指数")
    col = _seed_column(session, "akshare", "cpi_fun", "cpi_native_col")
    session.add(ConceptBinding(concept_id=c.id, column_id=col.id,
                               confidence=0.95, provenance="manual",
                               reviewed=True))
    session.commit()

    r = client.get(f"/panel/indicators/concepts/{c.id}")
    assert r.status_code == 200
    assert "跨源等价对" in r.text
    for native in ("W-CPI-1", "A-CPI-9", "A-CPI-10"):
        assert native in r.text          # every native code of the pair set
    # local binding rows appear with their column (native) names
    assert "cpi_native_col" in r.text
    assert "akshare" in r.text


# ── scenario 6: 映射可检索 — searchable mapping list ──────────────────────────

def test_relations_searchable(session):
    """映射可检索: filter by vocabulary, by SKOS relation, keyword over terms;
    htmx swaps the relations partial."""
    c1 = _seed_concept(session, "fd:GDP", name_zh="国内生产总值")
    c2 = _seed_concept(session, "fd:M2", name_zh="广义货币")
    mappings = [
        ConceptMapping(concept_id=c1.id, vocabulary="wikidata", term="Q11111",
                       relation="exact", confidence=0.9, provenance="manual"),
        ConceptMapping(concept_id=c1.id, vocabulary="wikidata", term="Q22222",
                       relation="close", confidence=0.8, provenance="llm"),
        ConceptMapping(concept_id=c2.id, vocabulary="datacommons",
                       term="DC_GDP_X", relation="exact", confidence=0.9,
                       provenance="manual"),
        ConceptMapping(concept_id=c2.id, vocabulary="datacommons",
                       term="DC_M2_Y", relation="broader", confidence=0.7,
                       provenance="llm"),
    ]
    session.add_all(mappings)
    session.commit()

    r = client.get("/panel/indicators/relations")
    assert r.status_code == 200
    for term in ("Q11111", "Q22222", "DC_GDP_X", "DC_M2_Y"):
        assert term in r.text

    # vocabulary filter: only that vocabulary's rows
    r = client.get("/panel/indicators/relations",
                   params={"vocabulary": "wikidata"})
    assert "Q11111" in r.text and "Q22222" in r.text
    assert "DC_GDP_X" not in r.text and "DC_M2_Y" not in r.text

    # relation filter: only exact rows across vocabularies
    r = client.get("/panel/indicators/relations", params={"relation": "exact"})
    assert "Q11111" in r.text and "DC_GDP_X" in r.text
    assert "Q22222" not in r.text and "DC_M2_Y" not in r.text

    # keyword over the term
    r = client.get("/panel/indicators/relations", params={"q": "Q1111"})
    assert "Q11111" in r.text
    assert "Q22222" not in r.text and "DC_GDP_X" not in r.text

    # htmx partial fragment
    r = client.get("/panel/indicators/relations", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text and "Q11111" in r.text


# ── scenario 7 + 10: bounded relation graph + node-detail contract ────────────

def _seed_graph_neighborhood(session):
    """Family fd:GDP with an ego concept (1 binding + 1 mapping) and a sibling."""
    fam = ConceptFamily(code="fd:GDP", name_zh="GDP", name_en="GDP")
    session.add(fam)
    session.flush()
    ego = _seed_concept(session, "fd:GDP.nominal", "fd:GDP", name_zh="名义GDP")
    sib = _seed_concept(session, "fd:GDP.ppp", "fd:GDP", name_zh="购买力平价GDP")
    col = _seed_column(session, "akshare", "gdp_fun", "gdp_col")
    session.add(ConceptBinding(concept_id=ego.id, column_id=col.id,
                               confidence=0.9, provenance="manual"))
    m = ConceptMapping(concept_id=ego.id, vocabulary="wikidata", term="Q33333",
                       relation="exact", confidence=0.95, provenance="manual")
    session.add(m)
    session.flush()
    session.commit()
    return ego, sib, col, m


def _graph(**params):
    r = client.get("/panel/indicators/graph.json", params=params)
    assert r.status_code == 200
    body = r.json()
    assert "error" not in body
    return body


def test_graph_neighborhood_views(session, monkeypatch):
    """Graph views: indicator ego view carries family + bindings + mappings at
    depth 1 and sibling concepts at depth 2; family view attaches relations
    only at depth 2; GRAPH_MAX_NODES caps the response and flags truncation."""
    ego, sib, col, m = _seed_graph_neighborhood(session)

    ego_ids = {n["id"] for n in _graph(indicator=ego.id, depth=1)["nodes"]}
    assert ego_ids == {f"concept:{ego.id}", "family:fd:GDP",
                       f"column:{col.id}", f"term:{m.id}"}

    ego2_ids = {n["id"] for n in _graph(indicator=ego.id, depth=2)["nodes"]}
    assert ego2_ids == ego_ids | {f"concept:{sib.id}"}

    fam1_ids = {n["id"] for n in _graph(family="fd:GDP", depth=1)["nodes"]}
    assert fam1_ids == {"family:fd:GDP", f"concept:{ego.id}",
                        f"concept:{sib.id}"}

    fam2_ids = {n["id"] for n in _graph(family="fd:GDP", depth=2)["nodes"]}
    assert fam2_ids == fam1_ids | {f"column:{col.id}", f"term:{m.id}"}

    # server-side cap: graph_neighborhood reads the module-global at call time
    monkeypatch.setattr(observatory, "GRAPH_MAX_NODES", 3)
    capped = _graph(family="fd:GDP", depth=2)
    assert capped["truncated"] is True
    assert len(capped["nodes"]) <= 3


def test_graph_domain_view(session):
    """Domain view over the registry: one domain node + its distinct semantic
    codes ordered by anchor count. Uses max(verified) — portable SQL, so the
    view works on the SQLite test backend exactly as on PostgreSQL."""
    _make_registry(session, [
        _reg("macro.gdp", "world_bank", "NY.GDP.MKTP", "macro", True, "GDP"),
        _reg("macro.gdp", "cnstats", "A0101", "macro", False, "GDP"),
        _reg("macro.pop", "world_bank", "SP.POP.TOTL", "macro", True, "人口"),
        _reg("legal.case", "wenshu_db", "CASE01", "legal", True, "案件"),
    ])
    body = _graph(domain="macro")
    ids = {n["id"] for n in body["nodes"]}
    assert ids == {"domain:macro", "indicator:macro.gdp",
                   "indicator:macro.pop"}
    gdp = next(n for n in body["nodes"] if n["id"] == "indicator:macro.gdp")
    assert gdp["anchors"] == 2  # both rows share the semantic code
    assert gdp["verified"] is True  # max() of True/False
    edges = {(e["from"], e["to"]) for e in body["edges"]}
    assert ("domain:macro", "indicator:macro.gdp") in edges


def test_graph_indicator_node_detail_contract(session):
    """节点详情: the indicator node dict carries the keys the JS detail panel
    renders — code/names/verified/binding_count/mapping_count."""
    ego, _sib, col, _m = _seed_graph_neighborhood(session)
    extra = ConceptMapping(concept_id=ego.id, vocabulary="wikidata",
                           term="Q99999", relation="close", confidence=0.8,
                           provenance="llm")
    session.add(extra)
    session.commit()

    body = _graph(indicator=ego.id)
    node = next(n for n in body["nodes"] if n["id"] == f"concept:{ego.id}")
    assert {"code", "name_zh", "name_en", "verified",
            "binding_count", "mapping_count"} <= set(node)
    assert node["code"] == "fd:GDP.nominal"
    assert node["binding_count"] == 1
    assert node["mapping_count"] == 2


# ── scenario 8 + 9: panel-ui 自托管 assets + 降级/无脚本 fallback ─────────────

def test_graph_page_assets_selfhosted(session):
    """自托管: every script/link asset is same-origin under /panel/static/
    (no CDN http(s) URL); the vendored vis-network file is actually served."""
    r = client.get("/panel/indicators/graph")
    assert r.status_code == 200

    srcs = re.findall(r'<script[^>]*\ssrc="([^"]+)"', r.text)
    assert srcs, "the graph page must reference its scripts"
    assert "/panel/static/vendor/vis-network.min.js" in srcs
    assert "/panel/static/indicator_graph.js" in srcs
    for src in srcs:
        assert src.startswith("/panel/static/"), src
        assert "http://" not in src and "https://" not in src

    hrefs = re.findall(r'<link[^>]*\shref="([^"]+)"', r.text)
    assert hrefs, "the shell links its stylesheet"
    for href in hrefs:
        # no remote host anywhere; local assets are panel-relative (the only
        # non-/panel/static URI allowed is the inline data: favicon)
        assert "http://" not in href and "https://" not in href
        assert not href.startswith("//")
        assert href.startswith("/panel/static/") or href.startswith("data:")

    assert client.get("/panel/static/vendor/vis-network.min.js").status_code == 200


def test_graph_noscript_fallback(session):
    """降级/无脚本: the graph page keeps a server-rendered 关系列表 relation
    listing inside <noscript> (with seeded mapping terms), while the scripted
    path has the graph canvas and node-detail panel."""
    c = _seed_concept(session, "fd:UNEMP", name_zh="失业率")
    session.add(ConceptMapping(concept_id=c.id, vocabulary="wikidata",
                               term="Q47478", relation="exact",
                               confidence=0.9, provenance="manual"))
    session.commit()

    r = client.get("/panel/indicators/graph")
    assert r.status_code == 200
    blocks = re.findall(r"<noscript>(.*?)</noscript>", r.text, re.S)
    assert blocks, "a noscript fallback must exist"
    listing = next(b for b in blocks if "关系列表" in b)
    assert "Q47478" in listing          # a seeded mapping term renders in it

    # the scripted path's surface exists too
    assert 'id="graph-canvas"' in r.text
    assert 'id="graph-detail"' in r.text


# ── scenario 11: missing-registry degradation ─────────────────────────────────

def test_missing_registry_degrades(session):
    """Fail-soft: without registry_entries the registry page shows the
    not-present notice (still 200), coverage its unavailable notice, and the
    domain graph responds with an explicit error payload, never a 500."""
    # session fixture: ORM tables only — no registry_entries created
    r = client.get("/panel/indicators")
    assert r.status_code == 200
    assert "注册目录表" in _norm(r.text) and "not present" in _norm(r.text)

    r = client.get("/panel/indicators/coverage")
    assert r.status_code == 200
    t = _norm(r.text)
    assert "无法统计" in t and "not present" in t
    assert "counts are unavailable" in t

    body = client.get("/panel/indicators/graph.json",
                      params={"domain": "macro"}).json()
    assert body.get("nodes") == []
    assert "error" in body


# ── scenario 12: scope 管理面 (wave 2 — B landed as scoping.py) ───────────────

def _seed_for_scopes(session):
    """Registry rows so a source_dbs-pinned scope validates non-empty."""
    _make_registry(session, [
        _reg("macro.gdp", "world_bank", "NY.GDP.MKTP", "macro", True, "GDP"),
        _reg("macro.pop", "world_bank", "SP.POP.TOTL", "macro", True, "人口"),
        _reg("yearbook.urban", "cnstats", "A0A01", "yearbook", True, "城镇化率"),
    ])


def test_scope_create_from_panel_usable_by_tool_functions(session):
    """「Create scope from the panel」: a scope created via the panel POST is
    immediately usable via the MCP scope tool path — the panel calls the same
    scoping service functions the tools call (design D5), so consistency is
    by construction; pinned here by resolving the scope exactly as a tool
    call would."""
    from fd_open_data_mcp import scoping

    _seed_for_scopes(session)
    r = client.post("/panel/indicators/scopes/create", data={
        "name": "wb-only", "description": "world_bank only",
        "source_dbs": "world_bank", "domains": "", "semantic_codes": "",
        "native_codes": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/panel/indicators/scopes")

    # the tool path (same function server.py's scope_* tools call) sees it
    listed = {s["name"]: s for s in scoping.scope_list(session)}
    assert "wb-only" in listed
    resolved = scoping.resolve_scope(session, explicit="wb-only")
    assert resolved["name"] == "wb-only"
    assert resolved["rules"]["source_dbs"] == ["world_bank"]

    # the list page renders it with its rules
    page = client.get("/panel/indicators/scopes")
    assert page.status_code == 200
    assert "wb-only" in page.text
    assert "world_bank" in page.text


def test_scope_empty_rules_refused(session):
    """Empty-scope guard through the panel: rules matching nothing known are
    refused with the explicit error surfaced."""
    _seed_for_scopes(session)
    r = client.post("/panel/indicators/scopes/create", data={
        "name": "bogus", "source_dbs": "no_such_db", "domains": "",
        "semantic_codes": "", "native_codes": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "err=" in r.headers["location"]
    follow = client.get("/panel/indicators/scopes")
    assert "bogus" not in follow.text


def test_scope_detail_stats_and_update_delete(session):
    """「Scope stats visible」+ update/delete roundtrip: the detail page shows
    hit statistics recorded through the shared record_hit path, rule edits
    persist, and deletion removes the scope."""
    from fd_open_data_mcp import scoping

    _seed_for_scopes(session)
    client.post("/panel/indicators/scopes/create", data={
        "name": "wb-only", "source_dbs": "world_bank", "domains": "",
        "semantic_codes": "", "native_codes": ""})
    scoping.record_hit(session, "wb-only", results_returned=7)
    scoping.record_hit(session, "wb-only", results_returned=3)

    detail = client.get("/panel/indicators/scopes/wb-only")
    assert detail.status_code == 200
    assert "命中统计" in detail.text
    assert "2" in detail.text and "10" in detail.text  # calls / results totals

    # update: narrow to one semantic code
    r = client.post("/panel/indicators/scopes/wb-only/update", data={
        "description": "", "source_dbs": "world_bank",
        "semantic_codes": "macro.gdp", "domains": "", "native_codes": ""},
        follow_redirects=False)
    assert r.status_code == 303
    sc = {s["name"]: s for s in scoping.scope_list(session)}["wb-only"]
    assert sc["rules"]["semantic_codes"] == ["macro.gdp"]

    # delete -> gone, detail 404
    assert client.post("/panel/indicators/scopes/wb-only/delete",
                       follow_redirects=False).status_code == 303
    assert client.get("/panel/indicators/scopes/wb-only").status_code == 404
    assert all(s["name"] != "wb-only" for s in scoping.scope_list(session))

