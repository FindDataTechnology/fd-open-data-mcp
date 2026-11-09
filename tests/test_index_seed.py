"""指数种子导入与交付闸（tasks 5.1-5.4）。

三条最要紧的断言：
1. **同码不串类**：``000300`` 必须解析到 index 实体，不得命中山厦门港务那只股票；
2. **不编数**：没有值的格子不落行，坏行进 rejected 并给原因；
3. **临时源交付必须带声明**：provisional 源对外引用缺声明即拒绝。
"""
from __future__ import annotations

import datetime as dt

import pytest

from fd_open_data_mcp.models import Concept, Entity, SemanticObservation
from fd_open_data_mcp.price_sources import (
    DeliveryAttributionMissing,
    LicensedAdapterNotConfigured,
    SeedRowError,
    assert_delivery_allowed,
    ensure_index_concepts,
    ensure_index_entity,
    import_index_seed,
    load_registry,
    normalize_index_symbol,
    required_attribution,
    resolve_licensed_adapter,
    seed_watermarks,
)


# ------------------------------------------------------------ 5.1 identity


def test_symbol_normalization_accepts_suffixed_and_bare():
    assert normalize_index_symbol("000300") == "000300"
    assert normalize_index_symbol("000300.SS") == "000300"
    assert normalize_index_symbol("399001.SZ") == "399001"
    with pytest.raises(SeedRowError):
        normalize_index_symbol("CSI300")


def test_index_entity_does_not_collide_with_same_code_stock(session):
    """实测同码碰撞：000300 既是沪深300，也是厦门港务（股票）。必须分开。"""
    stock = Entity(entity_type="stock", code="000300", name_zh="厦门港务")
    session.add(stock)
    session.commit()

    eid, created = ensure_index_entity(session, "000300", name_zh="沪深300指数")
    assert created is True
    assert eid != stock.id

    rows = session.query(Entity).filter(Entity.code == "000300").all()
    assert {r.entity_type for r in rows} == {"stock", "index"}


def test_ensure_index_entity_is_idempotent(session):
    first, created1 = ensure_index_entity(session, "000905")
    session.commit()
    second, created2 = ensure_index_entity(session, "000905")
    assert (first, created1) == (second, True)
    assert created2 is False


def test_index_concepts_are_separate_from_stock_concepts(session):
    stock_close = Concept(code="price.close", entity_type="stock", frequency="daily", unit="CNY")
    session.add(stock_close)
    session.commit()

    fields, created = ensure_index_concepts(session)
    assert "close" in fields
    assert fields["close"] != stock_close.id
    assert "price.close" in created

    idx = session.get(Concept, fields["close"])
    assert idx.entity_type == "index"
    assert idx.unit == "point"


# ------------------------------------------------------------ 5.2 import


def _seed_rows():
    return [
        {"date": "2026-07-09", "symbol": "000300.SS", "open": "3950.1", "close": "3970.5", "high": "3985.0", "low": "3940.2", "volume": "123456", "name": "沪深300指数"},
        {"date": "2026-07-10", "symbol": "000300.SS", "open": "3971.0", "close": "3980.1", "high": "3990.3", "low": "3960.0", "volume": "234567", "name": "沪深300指数"},
        {"date": "2026-07-10", "symbol": "399001.SZ", "open": "12000", "close": "12100", "high": "12150", "low": "11980", "volume": "98765", "name": "深证成指"},
    ]


def test_import_writes_observations_under_seed_source(session):
    report = import_index_seed(session, _seed_rows())
    assert report.rows_in == 3
    assert report.observations_written > 0
    assert report.rejected == []

    marks = seed_watermarks(session, codes=["000300", "399001"])
    assert marks["000300"]["observations"] == 10  # 5 fields (O/C/H/L/vol) × 2 days
    assert marks["000300"]["latest"] == "2026-07-10"
    assert marks["399001"]["observations"] == 5

    row = session.query(SemanticObservation).filter(
        SemanticObservation.source_used == "index-seed"
    ).first()
    assert row.entity_type == "index"
    assert row.granularity == "day"


def test_import_is_idempotent(session):
    first = import_index_seed(session, _seed_rows())
    second = import_index_seed(session, _seed_rows())
    assert first.observations_written > 0
    assert second.observations_written == 0
    assert second.observations_existing == first.observations_written


def test_missing_values_do_not_become_rows(session):
    """空值 = 没有观测，不是"值为零"。"""
    rows = [{"date": "2026-07-10", "symbol": "000300", "close": "3980.1", "high": ""}]
    report = import_index_seed(session, rows)
    assert report.observations_written == 1
    codes = [
        session.get(Concept, o.concept_id).code
        for o in session.query(SemanticObservation).all()
    ]
    assert codes == ["price.close"]


def test_bad_rows_are_rejected_with_reason_not_silently_dropped(session):
    rows = [
        {"date": "not-a-date", "symbol": "000300", "close": "1"},
        {"date": "2026-07-10", "symbol": "BAD", "close": "1"},
        {"date": "2026-07-10", "symbol": "000300", "close": "abc"},
        {"date": "2026-07-10", "symbol": "000300", "close": ""},
        {"date": "2026-07-10", "symbol": "000300", "close": "3980.1"},
    ]
    report = import_index_seed(session, rows)
    assert report.rows_in == 5
    assert len(report.rejected) == 4
    assert report.observations_written == 1
    reasons = " ".join(r["reason"] for r in report.rejected)
    assert "ISO" in reasons and "6-digit" in reasons and "not numeric" in reasons


def test_dry_run_writes_nothing(session):
    before = session.query(SemanticObservation).count()
    report = import_index_seed(session, _seed_rows(), dry_run=True)
    assert report.dry_run is True
    assert report.observations_written > 0          # 计划写入数
    assert session.query(SemanticObservation).count() == before


# ------------------------------------------------------- 5.3 licensed shape


def test_licensed_adapter_unconfigured_refuses_without_fallback():
    with pytest.raises(LicensedAdapterNotConfigured, match="no configured adapter"):
        resolve_licensed_adapter("tushare-pro")


# ------------------------------------------------- 5.4 delivery attribution


def test_provisional_source_requires_attribution():
    reg = load_registry()
    required = required_attribution(reg, "index-seed")
    assert required is not None
    assert "待持牌源替换" in required.statement

    with pytest.raises(DeliveryAttributionMissing, match="without an attribution"):
        assert_delivery_allowed(reg, "index-seed", None)

    got = assert_delivery_allowed(reg, "index-seed", "行情来源：index-seed")
    assert got is not None and got.source_id == "index-seed"


def test_legacy_source_is_not_deliverable_at_all():
    reg = load_registry()
    with pytest.raises(DeliveryAttributionMissing, match="not deliverable at all"):
        required_attribution(reg, "astock_daily")
