"""Tests for the cn-report registry optimization (optimize-cn-report-registry)."""
import pytest

from fd_open_data_mcp.catalog.providers import PROVIDERS
from fd_open_data_mcp.catalog.readers import read_dict
from fd_open_data_mcp.semantic.mapper_llm import propose_concept


def test_cnreport_seed_loads():
    records, errors = read_dict("fd_open_data_mcp.catalog.seeds.cn_report", "REGISTRY", "upstream-curated")
    assert errors == []
    cmds = {r["command"] for r in records}
    assert "get_financial_statements" in cmds and "extract_indicators" in cmds
    fs = next(r for r in records if r["command"] == "get_financial_statements")
    col_names = {c["name"] for c in fs["columns"]}
    assert "资产总计" in col_names and "营业收入" in col_names


def test_cnreport_provider_config():
    cfg = PROVIDERS["cn-report"]
    assert cfg["reader"] == "dict"
    assert cfg["dict_module"] == "fd_open_data_mcp.catalog.seeds.cn_report"
    assert cfg["scanner_mode"] == "upstream-curated"


def test_import_cnreport_has_columns(session):
    """cn-report tools import with >0 columns (unlike the old mcp-introspect)."""
    from fd_open_data_mcp.catalog.importer import import_provider
    from fd_open_data_mcp.models import Function, Source

    r = import_provider("cn-report", session)
    assert r["curated_count"] > 0
    src = session.query(Source).filter_by(name="cn-report").first()
    fns = session.query(Function).filter_by(source_id=src.id).all()
    assert fns and all(len(f.columns) > 0 for f in fns)


def test_read_cnreport_rules():
    from pathlib import Path

    from fd_open_data_mcp.catalog.cnreport_rules import default_cnreport_db, read_cnreport_rules

    if not Path(default_cnreport_db()).exists():
        pytest.skip("cn-report rules DB not present")
    rules, errors = read_cnreport_rules(None, module="balance_sheet", limit=5)
    assert errors == []
    assert len(rules) > 0
    assert all(r["module"] == "balance_sheet" for r in rules)
    assert "extract" in rules[0] and "indicator" in rules[0]


def test_read_cnreport_rules_missing_db(tmp_path):
    from fd_open_data_mcp.catalog.cnreport_rules import read_cnreport_rules

    rules, errors = read_cnreport_rules(str(tmp_path / "nope.db"))
    assert rules == []
    assert any("not found" in e for e in errors)


def test_cnreport_concept_mapping():
    assert propose_concept("资产总计")["code"] == "financials.total_assets"
    assert propose_concept("负债合计")["code"] == "financials.total_liabilities"
    assert propose_concept("所有者权益合计")["code"] == "financials.equity"
    assert propose_concept("经营活动现金流量")["code"] == "financials.operating_cash_flow"
