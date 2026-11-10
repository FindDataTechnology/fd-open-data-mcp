"""价格口径探针（tasks 2.1-2.4）。

这里测三件比"能跑"重要得多的事：

1. **只读**：跑完不产生任何文件；
2. **空比对不是通过**：两源没有同日重叠时必须报违规，不能返回零违规；
3. **今天能红**：按已实测的现状（astock 止于 2026-07-10、指数零观测、两源差 1.00 元）
   构造数据，探针必须失败。这条是"判据在做之前就绿＝没验收"的直接落地。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from fd_open_data_mcp.models import Concept, Entity, SemanticObservation
from scripts.price_caliber_gate import main


@pytest.fixture
def store(session, monkeypatch, tmp_path):
    """照实测现状造库：A 股两源同日差 1.00，指数族零观测。"""
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path/'gate.db'}")
    monkeypatch.delenv("MARKET_DATA_SOURCE_ID", raising=False)
    monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
    monkeypatch.delenv("ASTOCK_SOURCE", raising=False)
    monkeypatch.delenv("RESEARCH_PRICE_SOURCE", raising=False)
    monkeypatch.delenv("DAAS_PRICE_SOURCE", raising=False)
    monkeypatch.delenv("SETTLE_PRICE_SOURCE", raising=False)
    monkeypatch.delenv("DECISION_SETTLE_PRICE_SOURCE", raising=False)

    c = Concept(code="price.close", entity_type="stock", frequency="daily", unit="CNY")
    e = Entity(entity_type="stock", code="600036", name_zh="招商银行")
    session.add_all([c, e])
    session.flush()
    session.add_all(
        [
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-10", granularity="day", value="36.73",
                source_used="astock_daily",
            ),
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-10", granularity="day", value="37.73",
                source_used="akshare",
            ),
        ]
    )
    session.commit()
    return session


ARGS = ["--today", "2026-07-20", "--json"]


def _run(*extra, expect: int) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main([*ARGS, *extra])
    assert rc == expect, f"exit={rc}, want {expect}; output={buf.getvalue()[:500]}"
    return buf.getvalue()


def test_unspecified_paths_are_violations(store):
    out = _run("--trading", "astock_daily", expect=1)
    assert "PATH_SOURCE_UNSPECIFIED" in out
    assert "research" in out and "settle" in out


def test_unregistered_path_source_is_violation(store):
    out = _run(
        "--trading", "astock_daily", "--research", "nope", "--settle", "astock_daily",
        expect=1,
    )
    assert "PATH_SOURCE_UNREGISTERED" in out


def test_three_way_divergence_is_violation(store):
    out = _run(
        "--trading", "astock_daily", "--research", "akshare", "--settle", "astock_daily",
        expect=1,
    )
    assert "SOURCE_ID_DIVERGENCE" in out


def test_index_absence_is_red(store):
    out = _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        expect=1,
    )
    assert "INDEX_SERIES_ABSENT" in out


PACK_DECLARATIONS = (Path(__file__).resolve().parents[1]
                     / "fd_open_data_mcp/price_sources/declarations.yaml")


def test_caliber_divergence_is_red(store, tmp_path):
    """判据三能红的证据：**同锚**两源同日差 2.7% → 必红。

    必须用同锚声明：生产声明里 akshare 锚 per_fetch ≠ 首选源 2026-07-10，
    那种情况下的正确结论是「不可比」（CROSS_SOURCE_ANCHOR_MISMATCH），
    不该报价差；拿异锚声明验"能红"会把两条判据混成一件事。
    另测 `test_anchor_mismatch_is_reported_not_compared` 钉住异锚那半边。
    """
    decl = _same_anchor_registry(tmp_path, PACK_DECLARATIONS.read_text())
    out = _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        "--declarations", str(decl),
        expect=1,
    )
    assert "CALIBER_DIVERGENCE" in out
    assert "36.73" in out and "37.73" in out




def _same_anchor_registry(tmp_path, base_yaml):
    """产出一份"两源同锚"的声明副本。

    生产声明里 akshare 锚 per_fetch、首选源锚 2026-07-10 —— 异锚不可比，
    判据三会（正确地）报 CROSS_SOURCE_ANCHOR_MISMATCH。下面两条用例要验的是
    "空比对不等于通过"与"全绿"，前提必须是**可比**，所以这里把 akshare 的锚对齐。
    """
    import re
    # 声明是 **顶层键**（`akshare:` 不是 `source_id: akshare`），只改它的 adjustment_anchor
    text = re.sub(r"(\n  akshare:[\s\S]*?\n    adjustment_anchor: )per_fetch",
                  r'\1"2026-07-10"', base_yaml, count=1)
    out = tmp_path / "declarations-same-anchor.yaml"
    out.write_text(text)
    return out


def test_empty_overlap_is_not_a_pass(session, monkeypatch, tmp_path):
    """首选源在篮内无数据 → 跨源比对为空 → 必须报违规，不能零违规通过。"""
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path/'empty.db'}")
    c = Concept(code="price.close", entity_type="stock", frequency="daily", unit="CNY")
    e = Entity(entity_type="stock", code="600036", name_zh="招商银行")
    session.add_all([c, e])
    session.flush()
    session.add(
        SemanticObservation(
            concept_id=c.id, entity_type="stock", entity_id=e.id,
            date="2026-07-10", granularity="day", value="37.73", source_used="akshare",
        )
    )
    session.commit()
    decl = _same_anchor_registry(tmp_path, PACK_DECLARATIONS.read_text())
    out = _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        "--declarations", str(decl),
        expect=1,
    )
    assert "NO_CROSS_SOURCE_OVERLAP" in out


def test_green_when_everything_converges(session, monkeypatch, tmp_path):
    """全绿 = 同源同水位 + 指数有观测 + 无可比分歧 + **开关已切 refuse**。

    第四条是 change1 6.3 加的：只把数据理齐、开关还停在 warn，不算收敛完成——
    那等于把"口径不一致"永久降级成日志噪音。
    """
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path/'green.db'}")
    monkeypatch.setenv("PRICE_CALIBER_ENFORCEMENT", "refuse")
    c = Concept(code="price.close", entity_type="stock", frequency="daily", unit="CNY")
    ci = Concept(code="price.close", entity_type="index", frequency="daily", unit="point")
    e = Entity(entity_type="stock", code="600036", name_zh="招商银行")
    idx = Entity(entity_type="index", code="000300", name_zh="沪深300")
    session.add_all([c, ci, e, idx])
    session.flush()
    session.add_all(
        [
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-10", granularity="day", value="36.73",
                source_used="astock_daily",
            ),
            # 与首选源同日、口径相同 → 差 0，不违规
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-10", granularity="day", value="36.73",
                source_used="akshare",
            ),
            SemanticObservation(
                concept_id=ci.id, entity_type="index", entity_id=idx.id,
                date="2026-07-10", granularity="day", value="3980.1",
                source_used="index-seed",
            ),
        ]
    )
    session.commit()
    decl = _same_anchor_registry(tmp_path, PACK_DECLARATIONS.read_text())
    out = _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        "--declarations", str(decl),
        expect=0,
    )
    assert '"ok": true' in out


def test_gate_writes_nothing(store, tmp_path, monkeypatch):
    """spec: Gate writes nothing。跑完前后目录内容必须一致。"""
    before = {p.name for p in tmp_path.iterdir()}
    _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        expect=1,
    )
    after = {p.name for p in tmp_path.iterdir()}
    assert before == after


def test_env_fallback_picks_up_paths(store, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "astock_daily")
    monkeypatch.setenv("RESEARCH_PRICE_SOURCE", "astock_daily")
    monkeypatch.setenv("SETTLE_PRICE_SOURCE", "astock_daily")
    out = _run(expect=1)
    # 三路已从 env 解析到 → 不该再报"未配置"；剩下的红必须来自指数与价差
    assert "PATH_SOURCE_UNSPECIFIED" not in out
    assert "INDEX_SERIES_ABSENT" in out


# ------------------------------------ 判据四：开关断言（change1 6.3）


def test_warn_mode_is_a_violation(monkeypatch):
    """开关停在 warn 就是违规——它不是"暂时没关系"，是"还没收敛完"。"""
    from scripts.price_caliber_gate import judge_enforcement_mode

    monkeypatch.setenv("PRICE_CALIBER_ENFORCEMENT", "warn")
    vs, checked = judge_enforcement_mode()
    assert any(v.code == "ENFORCEMENT_STILL_WARN" for v in vs)
    assert checked["mode"] == "warn"


def test_unset_mode_defaults_to_warn_and_is_a_violation(monkeypatch):
    from scripts.price_caliber_gate import judge_enforcement_mode

    monkeypatch.delenv("PRICE_CALIBER_ENFORCEMENT", raising=False)
    vs, checked = judge_enforcement_mode()
    assert any(v.code == "ENFORCEMENT_STILL_WARN" for v in vs)
    assert checked["raw"] is None
    assert "未设置" in vs[0].detail


def test_invalid_mode_is_a_violation(monkeypatch):
    from scripts.price_caliber_gate import judge_enforcement_mode

    monkeypatch.setenv("PRICE_CALIBER_ENFORCEMENT", "maybe")
    vs, _ = judge_enforcement_mode()
    assert any(v.code == "ENFORCEMENT_MODE_INVALID" for v in vs)


def test_refuse_mode_produces_no_enforcement_violation(monkeypatch):
    from scripts.price_caliber_gate import judge_enforcement_mode

    monkeypatch.setenv("PRICE_CALIBER_ENFORCEMENT", "refuse")
    vs, checked = judge_enforcement_mode()
    assert vs == [] and checked["mode"] == "refuse"


def test_enforcement_violation_is_visible_even_when_db_is_unreachable(monkeypatch, tmp_path):
    """库不可达（exit 2）时，判据四的违规仍必须出现在输出里。

    这条钉的是设计意图：一个不依赖外部资源的判据，不该因为别的判据跑不了而沉默。
    """
    import contextlib
    import io

    from scripts.price_caliber_gate import main

    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path/'empty.db'}")
    monkeypatch.delenv("PRICE_CALIBER_ENFORCEMENT", raising=False)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = main(["--today", "2026-07-20"])
    assert rc == 2  # 库确实跑不起来
    assert "ENFORCEMENT_STILL_WARN" in err.getvalue()


def test_anchor_mismatch_is_reported_not_compared(store):
    """异锚两源不可直接比价：必须报 ANCHOR_MISMATCH，且**不许**报价差分歧。

    生产实测依据：akshare 锚 `per_fetch`、首选源锚 `2026-07-10`；同一标的比值一周内
    从 1.00415 漂到 1.02172（不是恒定缩放）。按 0.5% 阈值比会连报 64 天"分歧"，
    而那 64 条没人能处置——正确结论是"两种口径不可比"。
    """
    out = _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        expect=1,   # 用产线真实声明（异锚）
    )
    assert "CROSS_SOURCE_ANCHOR_MISMATCH" in out
    assert "per_fetch" in out          # 点名两侧锚点，别只说"不可比"
    assert "CALIBER_DIVERGENCE" not in out
