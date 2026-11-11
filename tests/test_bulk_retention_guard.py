"""批量直写脚本的留存收尾：三条判据 + 一条反旁路静态闸。

背景见 `fd_open_data_mcp.fetch.retention_bulk` 的模块注释：`scripts/bulk_ingest_*.py`
绕过 `fetch.cache` 直接 INSERT，契约里的 T/W 在那些族上从不生效。这里证明收尾真的
会合并/裁剪，并且证明"某支脚本又忘了调它"能被静态查出来。
"""

import pathlib

import pytest
from sqlalchemy import text

from fd_open_data_mcp.fetch.retention_bulk import (
    apply_bulk_retention,
    check_bulk_contracts,
    resolve_bulk_contracts,
)
from fd_open_data_mcp.fetch.retention import RetentionContractMissing
from fd_open_data_mcp.models import Concept, DataRetentionContract

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _concept(conn, code, entity_type="fund"):
    from fd_open_data_mcp.models import Concept as C
    from sqlalchemy.orm import Session

    s = Session(bind=conn)
    c = C(code=code, entity_type=entity_type, frequency="daily", unit="x", verified=True)
    s.add(c)
    s.commit()
    cid = c.id
    s.close()
    return cid


def _contract(conn, source, family, cls, window_days=None):
    from sqlalchemy.orm import Session

    s = Session(bind=conn)
    s.add(DataRetentionContract(
        source=source, family=family, retention_class=cls,
        retention_window_days=window_days, re_fetchable=True,
        upstream_history_mutable=False, volume_budget_mb=64))
    s.commit()
    s.close()


def _insert(conn, cid, date, value="1", source="akshare", et="fund", eid=1):
    conn.execute(text("""
        INSERT INTO semantic_observations
          (concept_id, entity_type, entity_id, date, granularity, value, unit,
           source_used, fetched_at)
        VALUES (:cid, :et, :eid, :d, 'day', :v, 'x', :src, '2026-11-11 00:00:00')
    """), {"cid": cid, "et": et, "eid": eid, "d": date, "v": value, "src": source})


def _dates(conn, cid, et="fund", eid=1):
    return [r[0] for r in conn.execute(text("""
        SELECT date FROM semantic_observations
         WHERE concept_id=:cid AND entity_type=:et AND entity_id=:eid
         ORDER BY date"""), {"cid": cid, "et": et, "eid": eid}).all()]


def test_t_family_keeps_only_latest_and_records_a_batch_revision(session):
    conn = session.connection()
    cid = _concept(conn, "aum_total")
    _contract(conn, "akshare", "aum_total", "T", 30)
    for d in ("2026-11-09", "2026-11-10", "2026-11-11"):
        _insert(conn, cid, d)

    res = apply_bulk_retention(conn, source="akshare", concept_ids=[cid])

    assert res["t_deleted"] == 2
    assert _dates(conn, cid) == ["2026-11-11"], "T 类只该留最新一条"
    revs = conn.execute(text("""
        SELECT revision_kind, point_count, range_start, range_end
          FROM observation_revisions WHERE concept_id=:cid"""), {"cid": cid}).all()
    assert len(revs) == 1 and revs[0][0] == "batch"
    assert revs[0][1] == 2 and (revs[0][2], revs[0][3]) == ("2026-11-09", "2026-11-10")


def test_w_family_drops_only_full_iso_dates_before_cutoff(session):
    conn = session.connection()
    cid = _concept(conn, "price", entity_type="stock")
    _contract(conn, "akshare", "price", "W", 730)
    _insert(conn, cid, "2000-01-03", et="stock")     # 远超窗口
    _insert(conn, cid, "2026-11-10", et="stock")     # 窗口内
    _insert(conn, cid, "1999", et="stock")           # 裸年：宁可留着，不猜方向

    res = apply_bulk_retention(conn, source="akshare", concept_ids=[cid], reason="test")

    assert res["w_deleted"] == 1
    assert sorted(_dates(conn, cid, et="stock")) == ["1999", "2026-11-10"]


def test_p_family_is_untouched(session):
    conn = session.connection()
    cid = _concept(conn, "reports", entity_type="stock")
    _contract(conn, "akshare", "reports", "P")
    for d in ("1990-01-01", "2026-11-10"):
        _insert(conn, cid, d, et="stock")

    res = apply_bulk_retention(conn, source="akshare", concept_ids=[cid])

    assert res == {"t_deleted": 0, "w_deleted": 0}
    assert len(_dates(conn, cid, et="stock")) == 2, "P 类一行都不许动"


def test_enforce_refuses_a_concept_without_contract(session, monkeypatch):
    monkeypatch.setenv("FD_RETENTION_GATE", "enforce")
    conn = session.connection()
    cid = _concept(conn, "uncontracted")

    with pytest.raises(RetentionContractMissing):
        check_bulk_contracts(conn, source="akshare", concept_ids=[cid])


def test_resolve_falls_back_to_source_wildcard(session):
    conn = session.connection()
    cid = _concept(conn, "whatever")
    _contract(conn, "akshare", "*", "T", 30)

    resolved = resolve_bulk_contracts(conn, source="akshare", concept_ids=[cid])

    assert resolved[cid]["contract"] is not None, "source 级 `*` 契约必须能兜住族级缺口"


def test_every_script_that_writes_observations_applies_retention():
    """反旁路静态闸：直写观测的脚本必须调 `apply_bulk_retention`。

    这是本模块存在的理由——规则靠"记得写"维持时，第五支脚本一定会漏。判据故意简单
    且宁可误报：文件里写了 ``semantic_observations``，且用的是批量写手法
    （``INSERT INTO semantic_observations`` 或 ``execute_values``），就必须出现
    ``apply_bulk_retention``。只读该表的报表/审计脚本不会被误判。

    放行名单里的每一条都必须写明理由，且必须**仍然匹配**判据——名单条目失效时会红，
    防止名单变成"抄一次就不用再想"的豁免。
    """
    allowlist = {
        # 淘汰工具本身就是"减少观测行数"的地方，它还负责打印回滚语句（那句 rollback
        # 模板里含 INSERT INTO semantic_observations，会命中判据）。它按契约搬运并
        # 自带 4.2 行数对账闸，不需要也不应该再套一层收尾。
        "evict_retention_window.py": "淘汰工具本身（回滚模板含 INSERT 字样）",
        # 把误挂的观测搬回原表——是"反向搬运"，不是新数据入口；搬回来的行本就通过过
        # 某种写入，这里再裁会二次改写别人的历史。
        "quarantine_misattached_observations.py": "从隔离表反向搬回，不是新写入",
        # 写的是 fdw 外部表（另一台库的 semantic_observations），留存契约归那个库管。
        "bulk_ingest_astock_daily.py": "写 xinru3 的外部表(ft_sem_obs_xinru3_w)，非本库",
    }

    offenders, matched_allow = [], set()
    for path in sorted(SCRIPTS.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "semantic_observations" not in src:
            continue
        if "INSERT INTO semantic_observations" not in src and "execute_values" not in src:
            continue
        if "apply_bulk_retention" in src:
            continue
        if path.name in allowlist:
            matched_allow.add(path.name)
            continue
        offenders.append(path.name)

    stale = set(allowlist) - matched_allow
    assert not stale, f"放行名单里有条目已不再命中判据（应删掉或改判据）：{sorted(stale)}"
    assert not offenders, (
        "这些脚本直接写 semantic_observations 却没有执行留存收尾："
        f"{offenders}——在同一个事务里补 apply_bulk_retention(...)")

