"""registry-transparent-read-and-scope tasks 1.1/1.2: transparent federated
reads for registry-only indicators — one test per spec scenario plus the
failure semantics (federation-unavailable / no-read-channel / local reads
unaffected). The business-mcp endpoint is mocked at the single call site
(``federation._call_domain_tool``); no network in tests."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from fd_open_data_mcp import federation
from fd_open_data_mcp.models import Concept, SemanticObservation


REGISTRY_DDL = """
CREATE TABLE registry_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_db VARCHAR(64),
  source_table VARCHAR(128),
  source_column VARCHAR(128),
  native_code VARCHAR(128),
  semantic_code VARCHAR(128),
  name_zh VARCHAR(255),
  name_en VARCHAR(255),
  unit VARCHAR(64),
  frequency VARCHAR(32),
  domain VARCHAR(64),
  verified INTEGER DEFAULT 0
)
"""


def _seed_registry(session, **kw):
    row = {
        "source_db": "yearbook_catalog", "source_table": "yb_data",
        "source_column": "value", "native_code": "10401",
        "semantic_code": "yb.total_population", "name_zh": "年末总人口",
        "name_en": "Year-end total population", "unit": "万人",
        "frequency": "yearly", "domain": "人口", "verified": 1,
    }
    row.update(kw)
    cols = ", ".join(row)
    marks = ", ".join(f":{k}" for k in row)
    session.execute(
        text(f"INSERT INTO registry_entries ({cols}) VALUES ({marks})"), row)
    session.commit()


@pytest.fixture
def registry_session(session):
    session.execute(text(REGISTRY_DDL))
    session.commit()
    yield session


@pytest.fixture(autouse=True)
def _flag_and_endpoint(monkeypatch):
    """Every test starts from an explicit flag/endpoint state (flag default
    is off — production starts there too)."""
    federation.reset_cooldown()
    monkeypatch.delenv(federation.FLAG_ENV, raising=False)
    monkeypatch.delenv(federation.ENDPOINT_ENV, raising=False)
    monkeypatch.setenv(federation.ENDPOINT_ENV, "http://business-mcp.test/mcp")
    yield
    federation.reset_cooldown()


YEARBOOK_PAYLOAD = {
    "results": [
        {"year": 2015, "region": "全国", "value": 137462.0, "unit": "万人"},
        {"year": 2016, "region": "全国", "value": 138271.0, "unit": "万人"},
    ],
    "count": 2, "truncated": False,
}


# ─── 1.1 透明读取: yearbook success path ────────────────────────────────────

def test_registry_only_yearbook_read_succeeds_transparently(registry_session, monkeypatch):
    """Spec concept-fetch「Registry-only read succeeds transparently」: the
    value returns via the yearbook channel in the standard read row shape."""
    _seed_registry(registry_session)
    seen = {}

    def fake_call(tool, args):
        seen["tool"], seen["args"] = tool, args
        return dict(YEARBOOK_PAYLOAD)

    monkeypatch.setattr(federation, "_call_domain_tool", fake_call)
    monkeypatch.setenv(federation.FLAG_ENV, "1")

    rows = federation.federated_read(
        registry_session, "yb.total_population", ["2015", "2016"])
    assert seen["tool"] == "yearbook_read"
    assert seen["args"]["indicator_id"] == 10401
    assert rows == [
        {"date": "2015", "value": 137462.0, "unit": "万人",
         "source_used": "yearbook_catalog"},
        {"date": "2016", "value": 138271.0, "unit": "万人",
         "source_used": "yearbook_catalog"},
    ]


def test_read_tool_routes_semantic_code_via_server(registry_session, monkeypatch):
    """The MCP tool accepts a registry semantic_code as concept_id and serves
    the same row shape (routing invisible beyond source_used)."""
    from fd_open_data_mcp import server

    _seed_registry(registry_session)
    monkeypatch.setattr(federation, "_call_domain_tool",
                        lambda tool, args: dict(YEARBOOK_PAYLOAD))
    monkeypatch.setenv(federation.FLAG_ENV, "1")

    rows = server.read("yb.total_population", dates=["2015"])
    assert rows[0]["value"] == 137462.0
    assert rows[0]["source_used"] == "yearbook_catalog"
    assert "error" not in rows[0]


def test_read_series_registry_path(registry_session, monkeypatch):
    _seed_registry(registry_session)
    monkeypatch.setattr(federation, "_call_domain_tool",
                        lambda tool, args: dict(YEARBOOK_PAYLOAD))
    monkeypatch.setenv(federation.FLAG_ENV, "1")

    from fd_open_data_mcp import server
    out = server.read_series("yb.total_population", start="2015", end="2016")
    assert out["count"] == 2
    assert [p["date"] for p in out["points"]] == ["2015", "2016"]
    assert out["points"][0]["value"] == 137462.0
    assert out["points"][0]["source_used"] == "yearbook_catalog"


def test_resolve_by_native_code(registry_session, monkeypatch):
    _seed_registry(registry_session)
    monkeypatch.setattr(federation, "_call_domain_tool",
                        lambda tool, args: dict(YEARBOOK_PAYLOAD))
    monkeypatch.setenv(federation.FLAG_ENV, "1")
    rows = federation.federated_read(registry_session, "10401", ["2015"])
    assert rows[0]["value"] == 137462.0


def test_unverified_entry_is_not_readable(registry_session):
    """The verified-only rule carries into reads: an unverified entry is
    invisible (falls through to the local not-found behavior)."""
    _seed_registry(registry_session, verified=0)
    assert federation.resolve_registry_entry(registry_session, "yb.total_population") is None


# ─── 1.2 失败语义 ────────────────────────────────────────────────────────────

def test_flag_default_off_is_explicit_not_silent(registry_session):
    """Flag off (the shipped default) is an explicit disabled error — never
    empty values masquerading as data."""
    _seed_registry(registry_session)
    rows = federation.federated_read(registry_session, "yb.total_population", ["2015"])
    assert rows[0]["error"] == "federated_read_disabled"
    assert "yearbook_catalog" in rows[0]["detail"]


def test_partial_rollout_flag_admits_only_listed_sources(registry_session, monkeypatch):
    _seed_registry(registry_session)
    _seed_registry(
        registry_session, source_db="world_bank", native_code="SP.POP.TOTL",
        semantic_code="wb.population_total", verified=1)
    monkeypatch.setenv(federation.FLAG_ENV, "yearbook_catalog")
    assert federation.federated_read_enabled("yearbook_catalog") is True
    assert federation.federated_read_enabled("world_bank") is False


def test_unsupported_source_db_names_the_source(registry_session):
    """Spec「Unsupported source database reports clearly」."""
    _seed_registry(registry_session, source_db="some_new_panel",
                   semantic_code="p.new_indicator", native_code="NP1")
    rows = federation.federated_read(registry_session, "p.new_indicator", ["2020"])
    assert rows[0]["error"] == "no_read_channel"
    assert rows[0]["source_db"] == "some_new_panel"


def test_federation_outage_surfaces_loud(registry_session, monkeypatch):
    """Spec「Federation outage surfaces」: unreachable business-mcp → explicit
    federation-unavailable error, not empty values or silent success."""
    _seed_registry(registry_session)
    monkeypatch.setenv(federation.FLAG_ENV, "1")

    def boom(tool, args):
        raise federation.FederationUnavailable("connection refused")

    monkeypatch.setattr(federation, "_call_domain_tool", boom)
    rows = federation.federated_read(registry_session, "yb.total_population", ["2015"])
    assert rows[0]["error"] == "federation_unavailable"

    out = federation.federated_read_series(
        registry_session, "yb.total_population", "2015", "2016")
    assert out["error"] == "federation_unavailable"


def test_unconfigured_endpoint_is_federation_unavailable(monkeypatch):
    monkeypatch.delenv(federation.ENDPOINT_ENV, raising=False)
    with pytest.raises(federation.FederationUnavailable):
        federation._call_domain_tool("yearbook_read", {})


def test_failure_cooldown_suppresses_hammering(monkeypatch):
    """After a connection failure the endpoint is not retried per-date; the
    cooldown windows the retries (design risk: 健康探测结果短暂缓存)."""
    monkeypatch.setenv(federation.COOLDOWN_ENV, "60")

    attempts = []

    def boom(url, tool, args):
        attempts.append(tool)
        raise ConnectionError("connection refused")

    monkeypatch.setattr(federation, "_attempt_call", boom)
    with pytest.raises(federation.FederationUnavailable):
        federation._call_domain_tool("yearbook_read", {})
    with pytest.raises(federation.FederationUnavailable, match="cooldown"):
        federation._call_domain_tool("yearbook_read", {})
    assert len(attempts) == 1  # second call hit the cooldown, not the network

    federation.reset_cooldown()
    with pytest.raises(federation.FederationUnavailable):
        federation._call_domain_tool("yearbook_read", {})
    assert len(attempts) == 2  # cooldown lifted: the network is retried


def test_local_concept_read_unaffected_by_federation_outage(registry_session, monkeypatch):
    """Spec「Local reads unaffected by federation outage」: the local
    concept-keyed cache+dispatch path completes normally with business-mcp
    down."""
    from fd_open_data_mcp import server

    concept = Concept(code="test.gdp", entity_type="country", unit="亿元",
                      frequency="yearly", verified=True)
    registry_session.add(concept)
    registry_session.flush()
    registry_session.add(SemanticObservation(
        concept_id=concept.id, entity_type="country", entity_id=1,
        date="2015", granularity="year", value="689052.1", unit="亿元",
        source_used="cached_source"))
    registry_session.commit()

    def boom(tool, args):
        raise AssertionError("local read must never touch the federation")

    monkeypatch.setattr(federation, "_call_domain_tool", boom)

    rows = server.read(concept.id, "country", 1, ["2015"])
    assert float(rows[0]["value"]) == 689052.1  # cache hits serve the stored value
    assert rows[0]["from_cache"] is True


def test_unknown_concept_id_keeps_local_error_behavior(registry_session):
    """Neither local nor registry → the pre-change error (concept not found)."""
    from fd_open_data_mcp import server

    with pytest.raises(ValueError, match="not found"):
        server.read(999999, "country", 1, ["2015"])


def test_world_bank_read_requires_entity(registry_session, monkeypatch):
    """wb_read has no all-countries mode; the parameter requirement is an
    explicit error, never a fabricated default."""
    _seed_registry(registry_session, source_db="world_bank",
                   native_code="SP.POP.TOTL", semantic_code="wb.population_total")
    monkeypatch.setenv(federation.FLAG_ENV, "1")

    rows = federation.federated_read(registry_session, "wb.population_total", ["2015"])
    assert rows[0]["error"] == "invalid_request"
    assert "entity" in rows[0]["detail"]

    monkeypatch.setattr(
        federation, "_call_domain_tool",
        lambda tool, args: {"results": [{"year": 2015, "country": "CHN",
                                         "country_name": "中国", "value": 137462.0,
                                         "unit": None}], "count": 1})
    rows = federation.federated_read(
        registry_session, "wb.population_total", ["2015"], entity="CHN")
    assert rows[0]["value"] == 137462.0
