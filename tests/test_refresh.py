"""Tests for scheduled-refresh (scheduler + runner)."""
import pandas as pd
import pytest

import fd_open_data_mcp.fetch.dispatch as dispatch_mod
import fd_open_data_mcp.fetch.instrumentation as instr_mod
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, EntitySourceIdentifier, Execution, FetchLog,
    Function, FunctionColumn, Schedule, SemanticObservation, Source,
)
from fd_open_data_mcp.refresh.runner import refresh_concept, run_schedule
from fd_open_data_mcp.refresh.scheduler import generate_schedules


def _setup_price_close(session):
    src = Source(name="akshare", label="ak")
    session.add(src); session.flush()
    fn = Function(source_id=src.id, command="stock_zh_a_hist", verified=True,
                  scanner_mode="upstream-curated", frequency="daily",
                  parameters=[{"name": "symbol", "required": True}])
    session.add(fn); session.flush()
    col = FunctionColumn(function_id=fn.id, name="收盘")
    session.add(col); session.flush()
    c = Concept(code="price.close", entity_type="stock", measure="",
                unit="currency", frequency="daily")
    session.add(c); session.flush()
    session.add(ConceptBinding(concept_id=c.id, column_id=col.id, confidence=0.9,
                               provenance="manual", reviewed=True))
    session.add(EntitySourceIdentifier(entity_type="stock", entity_id=1, source="akshare", identifier="600519"))
    session.commit()
    return c.id, fn.id


def test_generate_schedules(session):
    c = Concept(code="gdp", entity_type="country", measure="nominal_current", unit="usd", frequency="yearly")
    session.add(c); session.commit()
    r = generate_schedules(session)
    assert r["created"] >= 1
    sched = session.query(Schedule).filter_by(concept_id=c.id).first()
    assert sched is not None
    r2 = generate_schedules(session)
    assert r2["created"] == 0  # idempotent


def test_refresh_concept_success(session, monkeypatch):
    cid, _ = _setup_price_close(session)
    df = pd.DataFrame({"日期": ["2024-07-26"], "收盘": [1850.0]})
    monkeypatch.setattr(instr_mod, "run_upstream", lambda s, c, p: df)
    r = refresh_concept(session, cid, "stock", 1, "2024-07-26")
    assert r["status"] == "success"
    assert session.query(Execution).filter_by(concept_id=cid, status="success").first() is not None
    assert session.query(FetchLog).filter_by(concept_id=cid).first() is not None
    assert session.query(SemanticObservation).filter_by(concept_id=cid, date="2024-07-26").first() is not None


def test_refresh_concept_failure(session, monkeypatch):
    cid, _ = _setup_price_close(session)

    def _fail(s, c, p):
        raise dispatch_mod.FetchError("boom")

    monkeypatch.setattr(instr_mod, "run_upstream", _fail)
    r = refresh_concept(session, cid, "stock", 1, "2024-07-26")
    assert r["status"] == "failed"
    assert session.query(Execution).filter_by(concept_id=cid, status="failed").first() is not None


def test_run_schedule(session, monkeypatch):
    cid, _ = _setup_price_close(session)
    generate_schedules(session)
    sched = session.query(Schedule).filter_by(concept_id=cid).first()
    df = pd.DataFrame({"日期": ["2024-07-26"], "收盘": [1850.0]})
    monkeypatch.setattr(instr_mod, "run_upstream", lambda s, c, p: df)
    refresh_concept(session, cid, "stock", 1, "2024-07-26")  # cache an obs
    r = run_schedule(session, sched.id)
    assert r["schedule_id"] == sched.id
