"""写入前的 W 窗口裁剪必须尊重豁免（`_drop_out_of_window`）。

为什么单独一条：这条路径不是"删库里的行"，而是**把点挡在写入之前**。被挡下的点
连库都进不去，重抓也补不回来——比写完再删更狠。2026-11-12 实测：`akshare/aum`
的豁免此前只对离线淘汰工具生效，这条路径照样按 1826 天窗口丢点，
等于"声明保留"在真正会丢数据的路径上没人看。
"""

from datetime import date, timedelta

from fd_open_data_mcp.fetch.cache import _drop_out_of_window
from fd_open_data_mcp.models import Concept, DataRetentionContract


def _setup(session, *, until, window_days=30):
    c = Concept(code="aum.nav", entity_type="fund", frequency="daily",
                unit="x", verified=True)
    session.add(c)
    session.add(DataRetentionContract(
        source="akshare", family="aum", retention_class="W",
        retention_window_days=window_days, re_fetchable=True,
        upstream_history_mutable=False, volume_budget_mb=64,
        exempt_reason="可重取但需重算 1128 小时", exempt_owner="chengsishi",
        exempt_until=until))
    session.commit()
    return c.id


ROWS = {"2000-01-03": 1.0, "2026-11-10": 2.0}


def test_active_exemption_keeps_out_of_window_points_at_write_time(session):
    cid = _setup(session, until=date.today() + timedelta(days=10))
    kept = _drop_out_of_window(session, cid, "fund", 1, "akshare", dict(ROWS))
    assert sorted(kept) == ["2000-01-03", "2026-11-10"], \
        "豁免期内不许在入口丢掉超窗点"


def test_expired_exemption_restores_entry_trimming(session):
    cid = _setup(session, until=date.today() - timedelta(days=1))
    kept = _drop_out_of_window(session, cid, "fund", 1, "akshare", dict(ROWS))
    assert list(kept) == ["2026-11-10"], "豁免到期后入口裁剪必须恢复"


def test_no_exemption_still_trims(session):
    cid = _setup(session, until=None)
    kept = _drop_out_of_window(session, cid, "fund", 1, "akshare", dict(ROWS))
    assert list(kept) == ["2026-11-10"]


def test_bare_year_point_is_never_dropped(session):
    """裸年比较没有唯一方向，宁可留着——这条不能被豁免改动顺手带掉。"""
    cid = _setup(session, until=None)
    kept = _drop_out_of_window(session, cid, "fund", 1, "akshare",
                               {"1999": 1.0, "2026-11-10": 2.0})
    assert sorted(kept) == ["1999", "2026-11-10"]
