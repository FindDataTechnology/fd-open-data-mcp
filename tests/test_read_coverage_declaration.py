"""读面的覆盖与降级声明（define-ingest-revision-and-coverage）。

两条要害：
① 窗口只覆盖一部分时，`missing` 必须列出来——不能与"完整"返回同一个形状。
② 回源全失败退回缓存时，`degraded` 必须为真——"取不到"不能长得像"没有"。

这里不重测补数算法（那是 gapfill 的用例），只测"声明是否如实"。
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.fetch.dispatch import read_range_with_coverage, read_range
from fd_open_data_mcp.models import Concept, SemanticObservation


@pytest.fixture
def cid(session) -> int:
    c = Concept(code="price.close", entity_type="stock", frequency="daily",
                unit="currency", verified=True)
    session.add(c)
    session.commit()
    return c.id


def _seed(session, cid, dates_and_values, *, source="akshare", fetched_at=None):
    for d, v in dates_and_values:
        session.add(SemanticObservation(
            concept_id=cid, entity_type="stock", entity_id=42, date=d,
            granularity="day", value=str(v), unit="currency", source_used=source,
            fetched_at=fetched_at or __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc),
        ))
    session.commit()


def test_partial_window_reports_missing(session, cid):
    """窗口 01-01..01-10 只有 01-01..01-03：`missing` 必须列出其余区间。"""
    _seed(session, cid, [(f"2026-01-0{i}", i) for i in range(1, 4)])

    got = read_range_with_coverage(session, [cid], "stock", 42, "2026-01-01", "2026-01-10")[cid]

    assert got["coverage"]["requested"] == ["2026-01-01", "2026-01-10"]
    assert got["coverage"]["covered"] == [["2026-01-01", "2026-01-03"]]
    assert got["coverage"]["missing"], "缺的区间必须列出来"
    merged = [d for lo, hi in got["coverage"]["missing"] for d in (lo, hi)]
    assert "2026-01-10" in merged, "缺口应覆盖到请求区间的右端"
    assert got["coverage"]["complete"] is False


def test_complete_window_says_complete(session, cid):
    """连续覆盖整窗：`missing` 为空且 complete 为真。"""
    _seed(session, cid, [(f"2026-01-0{i}", i) for i in range(1, 6)])

    got = read_range_with_coverage(session, [cid], "stock", 42, "2026-01-01", "2026-01-05")[cid]

    assert got["coverage"]["missing"] == []
    assert got["coverage"]["complete"] is True
    assert got["degraded"]["degraded"] is False


def test_no_rows_anywhere_is_missing_not_quiet(session, cid):
    """冷序列：整窗都是缺口，且不是因为降级。"""
    got = read_range_with_coverage(session, [cid], "stock", 42, "2026-01-01", "2026-01-05")[cid]

    assert got["values"] == []
    assert got["coverage"]["missing"] == [["2026-01-01", "2026-01-05"]]
    assert got["degraded"]["degraded"] is False, "没数据不等于读失败"


def test_stale_cache_with_failed_upstream_is_degraded(session, cid, monkeypatch):
    """关键用例：回源全失败、退回陈旧缓存 → degraded 为真，且给出原因。

    注意必须用**当期**日期：定型后的行按 `_period_final` 永不重取（is_stale 为
    False），它压根不会走回源路径。会"回源失败"的只有当期那些仍可修订的点。
    """
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    old = datetime.now(timezone.utc) - timedelta(days=30)
    _seed(session, cid, [(today, 7.0)], fetched_at=old)  # 当期 + 抓取很旧 → 陈旧

    def _boom(*a, **kw):
        from fd_open_data_mcp.fetch.instrumentation import SourceUnavailable
        raise SourceUnavailable("upstream down")

    monkeypatch.setattr("fd_open_data_mcp.fetch.dispatch.instrumented_fetch", _boom)

    got = read_range_with_coverage(session, [cid], "stock", 42, today, today)[cid]

    assert got["degraded"]["degraded"] is True, "取不到数据必须自证"
    assert got["degraded"]["reason"], "降级必须给原因"
    assert got["served_from"] in ("cache_degraded", "none")


def test_values_shape_matches_read_range(session, cid):
    """新入口的 values 与旧 read_range 逐行一致——只加声明，不改数据。"""
    _seed(session, cid, [(f"2026-01-0{i}", float(i)) for i in range(1, 4)])

    legacy = read_range(session, [cid], "stock", 42, "2026-01-01", "2026-01-03")[cid]
    new = read_range_with_coverage(session, [cid], "stock", 42,
                                   "2026-01-01", "2026-01-03")[cid]

    assert new["values"] == legacy, "兼容性：值必须一模一样，不许顺手改形状"


def test_empty_request_returns_empty_mapping(session, cid):
    assert read_range_with_coverage(session, [], "stock", 42, "2026-01-01", "2026-01-02") == {}
    assert read_range_with_coverage(session, [cid], "stock", 42,
                                    "2026-01-05", "2026-01-01") == {}


# ---- series_dates 的等价性与"只取日期"（decision 2C）----

def test_series_dates_matches_old_full_row_scan(session, cid):
    """窄化成 DISTINCT date 后，结果必须与"取全部行再挑日期"逐项相同。"""
    from fd_open_data_mcp.fetch.cache import _read_base_range
    from fd_open_data_mcp.fetch.gapfill import series_dates

    _seed(session, cid, [("2026-01-01", 1), ("2026-01-03", 3), ("2026-01-02", 2)],
          source="akshare")
    _seed(session, cid, [("2026-01-02", 9), ("2026-01-05", 5)], source="polygon")

    old = sorted({r.date for r in _read_base_range(
        session, cid, "stock", 42, "0000-01-01", "9999-12-31")})
    new = series_dates(session, cid, "stock", 42)

    assert new == old, "跨来源的日期集合必须一致"
    assert new == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-05"]


def test_series_dates_is_ordered_and_deduplicated(session, cid):
    from fd_open_data_mcp.fetch.gapfill import series_dates

    _seed(session, cid, [("2026-01-02", 1)], source="a")
    _seed(session, cid, [("2026-01-02", 2)], source="b")  # 同一天两个来源

    got = series_dates(session, cid, "stock", 42)
    assert got == ["2026-01-02"], "同一天不得重复出现"
    assert got == sorted(got)


def test_series_dates_empty_series(session, cid):
    from fd_open_data_mcp.fetch.gapfill import series_dates

    assert series_dates(session, cid, "stock", 42) == []


def test_trail_gap_is_clipped_to_the_requested_window(session, cid):
    """窗口整体落在序列末尾之后：缺口必须从窗口起点算，不许报出窗口外。

    这是 coverage() 的一个真 bug（2026-11-09 由 read_series 的覆盖声明用例发现）：
    trail 缺口原本是 `(span_end+1, end)`，没裁左端，于是请求 02-01..02-05 会报
    "缺 01-04..02-05"——既多报了用户没要求的时段，也会让补数去问窗口之前的日期。
    """
    from fd_open_data_mcp.fetch.coverage import coverage

    rep = coverage(["2026-01-01", "2026-01-02", "2026-01-03"],
                   "2026-02-01", "2026-02-05")

    assert rep.gaps == (("2026-02-01", "2026-02-05"),), (
        f"缺口必须被裁到窗口内，实际 {rep.gaps}"
    )


def test_trail_gap_starting_inside_window_is_unchanged(session, cid):
    """窗口跨过序列末尾（起点早于末尾）：缺口仍从末尾次日起算。"""
    from fd_open_data_mcp.fetch.coverage import coverage

    rep = coverage(["2026-01-01", "2026-01-03"], "2026-01-01", "2026-01-10")

    assert rep.gaps == (("2026-01-04", "2026-01-10"),)
