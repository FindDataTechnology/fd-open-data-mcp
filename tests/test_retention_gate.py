"""留存闸门的三档行为与 family 口径。

这里守的是**上线顺序**：默认必须是 off。存量有 72 个 (源, 族) 未声明契约，
若默认就 enforce，这些源的爬取当晚全线停摆——那不是严格，是事故。
"""

import pathlib

import pytest

from fd_open_data_mcp.fetch import retention
from fd_open_data_mcp.models import Concept, DataRetentionContract


def _concept(session, code="financials.revenue"):
    c = Concept(code=code, entity_type="stock", frequency="yearly",
                unit="CNY", verified=True)
    session.add(c)
    session.commit()
    return c


def test_default_mode_is_off(monkeypatch):
    """没有任何环境变量时必须 off——否则部署即静默改行为。"""
    monkeypatch.delenv("FD_RETENTION_GATE", raising=False)
    assert retention.gate_mode() == "off"


def test_off_mode_never_queries_and_never_raises(session, monkeypatch):
    monkeypatch.delenv("FD_RETENTION_GATE", raising=False)
    c = _concept(session)
    # 没有契约也不该抛（off 档零检查）
    retention.check_write_allowed(session, concept_id=c.id, source="akshare")


def test_warn_mode_logs_but_allows(session, monkeypatch, caplog):
    monkeypatch.setenv("FD_RETENTION_GATE", "warn")
    c = _concept(session)
    with caplog.at_level("WARNING"):
        retention.check_write_allowed(session, concept_id=c.id, source="akshare")
    # 用 caplog.text（已格式化）判断，不要自己 % args——那会在无参数时炸
    assert "留存契约缺失" in caplog.text
    assert "warn 档" in caplog.text


def test_enforce_mode_refuses_without_contract(session, monkeypatch):
    monkeypatch.setenv("FD_RETENTION_GATE", "enforce")
    c = _concept(session)
    with pytest.raises(retention.RetentionContractMissing) as ei:
        retention.check_write_allowed(session, concept_id=c.id, source="akshare")
    assert ei.value.family == "financials"
    assert ei.value.source == "akshare"


def test_enforce_mode_allows_when_contract_declared(session, monkeypatch):
    monkeypatch.setenv("FD_RETENTION_GATE", "enforce")
    c = _concept(session)
    session.add(DataRetentionContract(source="akshare", family="financials",
                                      retention_class="F", re_fetchable=True,
                                      upstream_history_mutable=True,
                                      volume_budget_mb=3072))
    session.commit()
    retention.check_write_allowed(session, concept_id=c.id, source="akshare")


def test_source_wildcard_contract_applies(session, monkeypatch):
    """族未单列时回落到该源的 `*` 契约。"""
    monkeypatch.setenv("FD_RETENTION_GATE", "enforce")
    c = _concept(session, code="gdp.total")
    session.add(DataRetentionContract(source="worldbank", family="*",
                                      retention_class="F", re_fetchable=True,
                                      upstream_history_mutable=True,
                                      volume_budget_mb=64))
    session.commit()
    retention.check_write_allowed(session, concept_id=c.id, source="worldbank")


def test_declared_but_empty_class_still_counts_as_missing(session, monkeypatch):
    """只填了源和族、class 仍为 NULL → 仍算未声明（NULL 不等于"决定永久保留"）。"""
    monkeypatch.setenv("FD_RETENTION_GATE", "enforce")
    c = _concept(session)
    session.add(DataRetentionContract(source="akshare", family="financials"))
    session.commit()
    with pytest.raises(retention.RetentionContractMissing):
        retention.check_write_allowed(session, concept_id=c.id, source="akshare")


def test_family_is_first_segment_of_code():
    """族口径必须与审计器一致：概念码第一段。两处不一致会造出自相矛盾的结论。"""
    assert retention.family_of("financials.revenue") == "financials"
    assert retention.family_of("price") == "price"
    assert retention.family_of(None) is None
    other = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "audit_retention_contracts.py"
    src = other.read_text()
    assert "split_part(co.code, '.', 1)" in src
