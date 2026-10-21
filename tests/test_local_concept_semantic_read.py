"""Reproduction for the latent defect that industry-vertical-supply D2
documents: a verified registry entry whose source_db has no federated read
channel is advertised as 可直读 (spec wire-indicator-catalog「状态分级如实
呈现」) yet returns ``no_read_channel`` when addressed by semantic code.

The concrete case is the ``fd_open_data`` mirror adapter: it registers every
local concept into ``registry_entries`` with ``source_db='fd_open_data'``,
and 'fd_open_data' is absent from ``SOURCE_DB_CHANNELS``. So every concept in
the dictionary becomes unreadable by semantic code the moment the mirror runs.

Task 1.1 is to prove it. These three tests MUST fail before the fix and pass
after it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from fd_open_data_mcp import federation, server
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
  provenance TEXT,
  verified INTEGER DEFAULT 0
)
"""


@pytest.fixture
def registry_session(session):
    session.execute(text(REGISTRY_DDL))
    session.commit()
    yield session


def _seed_local_and_mirror(session, code="express.volume"):
    """A normal situation after the fd_open_data adapter has run: the concept
    exists locally (data included) AND is mirrored into registry_entries."""
    concept = Concept(code=code, entity_type="country", unit="亿件",
                      frequency="monthly", verified=True)
    session.add(concept)
    session.flush()
    for period, value in (("2026-08-01", "155.5"), ("2026-07-01", "142.3")):
        session.add(SemanticObservation(
            concept_id=concept.id, entity_type="country", entity_id=1,
            date=period, granularity="month", value=value, unit="亿件",
            source_used="spb-express"))
    session.execute(text(
        "INSERT INTO registry_entries (source_db, source_table, source_column,"
        " native_code, semantic_code, name_zh, name_en, unit, frequency,"
        " domain, verified) VALUES ('fd_open_data', 'concepts', 'code',"
        " :code, :code, '快递业务量', 'Express delivery volume', '亿件',"
        " 'monthly', '物流', 1)"), {"code": code})
    session.commit()
    return concept


def test_verified_mirror_entry_readable_by_semantic_code(registry_session):
    """Task 1.1 — the core defect. Verified entry + real local data, yet the
    semantic-code read fails."""
    _seed_local_and_mirror(registry_session)
    rows = server.read("express.volume", "country", 1, ["2026-08-01"])
    assert rows[0].get("error") is None, rows[0]
    assert float(rows[0]["value"]) == 155.5


def test_verified_mirror_entry_series_by_semantic_code(registry_session):
    """Same defect through the series entry point."""
    _seed_local_and_mirror(registry_session)
    out = server.read_series("express.volume", entity_type="country",
                             entity_id=1, start="2026-07-01", end="2026-08-31")
    assert out.get("error") is None, out
    assert out["count"] == 2


def test_numeric_local_read_still_works(registry_session):
    """Regression guard: the numeric-id path already worked and must keep
    working after the fallback lands."""
    concept = _seed_local_and_mirror(registry_session)
    rows = server.read(concept.id, "country", 1, ["2026-08-01"])
    assert float(rows[0]["value"]) == 155.5
    assert rows[0]["from_cache"] is True


def test_local_concept_does_not_hijack_a_source_that_owns_a_channel(
        registry_session, monkeypatch):
    """A source_db WITH a federated channel keeps routing federated even when
    a local concept shares the code — otherwise this fix would silently move
    world_bank / gta_panel traffic onto local data."""
    _seed_local_and_mirror(registry_session, code="yb.total_population")
    registry_session.execute(text(
        "UPDATE registry_entries SET source_db='yearbook_catalog',"
        " native_code='10401'"))
    registry_session.commit()
    monkeypatch.setenv(federation.FLAG_ENV, "1")

    calls = []

    def fake_call(tool, args):
        calls.append(tool)
        return {"results": [{"date": "2015-12-31", "value": 139008.0}]}

    monkeypatch.setattr(federation, "_call_domain_tool", fake_call)

    rows = server.read("yb.total_population", "country", 1, ["2015-12-31"])
    assert calls[0] == "yearbook_regions", "region discovery precedes the read"
    assert calls[-1] == "yearbook_read", "must still use the federated channel"


def test_unresolvable_entry_keeps_no_read_channel(registry_session):
    """A channel-less source with no matching local concept keeps the loud
    error — the fallback must not turn real gaps into silent empties."""
    registry_session.execute(text(
        "INSERT INTO registry_entries (source_db, source_table, source_column,"
        " native_code, semantic_code, name_zh, name_en, unit, frequency,"
        " domain, verified) VALUES ('some_new_panel', 't', 'v', 'NP1',"
        " 'p.new_indicator', '某指标', 'Some indicator', '', 'monthly',"
        " '产业', 1)"))
    registry_session.commit()
    rows = server.read("p.new_indicator", "country", 1, ["2026-08-01"])
    assert rows[0]["error"] == "no_read_channel"
