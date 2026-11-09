"""as-of 回放的判定规则（对应 make-observation-vintage-readable 任务 4.1–4.3）。

四态必须可区分，且任何不可精确回放的情形都**不得**用当前值冒充：

    exact               可精确重建
    batch_unreplayable  落在整段修订区间，逐点旧值按设计未留存
    vintage_unavailable 早于首条可追溯修订且旧值未留存
    absent              该时点尚不存在（首次写入更晚）
"""

from datetime import datetime, timedelta, timezone

import pytest

from fd_open_data_mcp.fetch.vintage import (
    ABSENT,
    BATCH_UNREPLAYABLE,
    EXACT,
    VINTAGE_UNAVAILABLE,
    Revision,
    normalize_as_of,
    replay_point,
    summarize,
)


def t(y, m, d, h=12):
    """naive UTC 时点，与库内一致。"""
    return datetime(y, m, d, h)


# --- 4.1 单点修订精确重放 ---------------------------------------------------

def test_overwrite_chain_replays_intermediate_value():
    """T1→v2、T2→v3：问 T1.5 得 v2，问 T2.5 得 v3。"""
    revs = [
        Revision(revised_at=t(2024, 6, 1), old_value="v1", new_value="v2", revision_id=1),
        Revision(revised_at=t(2024, 9, 1), old_value="v2", new_value="v3", revision_id=2),
    ]

    at_july = replay_point("2023-12-31", "year", "v3", t(2024, 10, 1), revs, "2024-07-01")
    assert at_july.value == "v2"
    assert at_july.status == EXACT

    at_oct = replay_point("2023-12-31", "year", "v3", t(2024, 10, 1), revs, "2024-10-01")
    assert at_oct.value == "v3"
    assert at_oct.status == EXACT


def test_before_any_revision_replays_original_value():
    """早于首条修订 → 当时的值就是被替换掉的 old_value。"""
    revs = [Revision(revised_at=t(2024, 6, 1), old_value="orig", new_value="v2", revision_id=1)]

    out = replay_point("2023-12-31", "year", "v2", None, revs, "2024-01-01")

    assert out.value == "orig"
    assert out.status == EXACT


def test_unrevised_point_is_exact_when_written_before_as_of():
    out = replay_point("2020-12-31", "year", "5.2", t(2021, 2, 1), [], "2022-01-01")

    assert out.value == "5.2"
    assert out.status == EXACT


# --- 4.2 batch 区间显式不可回放 ---------------------------------------------

def test_batch_covered_point_is_flagged_not_faked():
    """前复权整段重写：逐点旧值没留，必须显式说不可回放，不能返回当前值。"""
    revs = [Revision(
        revised_at=t(2024, 5, 1), old_value=None, new_value=None,
        revision_kind="batch", batch_id="adj-2024-05",
        range_start="2020-01-01", range_end="2024-04-30", revision_id=1,
    )]

    # 整段修订发生在 2024-05-01，请求时点在其之后 → 覆盖成立，不可逐点回放
    out = replay_point("2021-06-15", "day", "999.0", t(2024, 6, 1), revs, "2024-06-01")

    assert out.status == BATCH_UNREPLAYABLE
    assert out.value is None, "不得拿当前值冒充当时值"
    assert out.batch_id == "adj-2024-05"


def test_batch_after_as_of_does_not_block_replay():
    """整段修订发生在请求时点之后 → 不影响当时的值。"""
    revs = [
        Revision(revised_at=t(2024, 1, 1), old_value="1.0", new_value="1.5", revision_id=1),
        Revision(revised_at=t(2024, 5, 1), old_value=None, new_value=None,
                 revision_kind="batch", batch_id="b2",
                 range_start="2020-01-01", range_end="2024-04-30", revision_id=2),
    ]

    out = replay_point("2021-06-15", "day", "2.0", None, revs, "2024-03-01")

    assert out.status == EXACT
    assert out.value == "1.5"


# --- 4.3 无历史显式声明 -----------------------------------------------------

def test_point_before_tracking_without_old_value_is_unavailable():
    revs = [Revision(revised_at=t(2024, 6, 1), old_value=None, new_value="v2", revision_id=1)]

    out = replay_point("2019-12-31", "year", "v2", None, revs, "2020-01-01")

    assert out.status == VINTAGE_UNAVAILABLE
    assert out.value is None


def test_never_revised_and_no_write_time_is_unavailable():
    out = replay_point("2019-12-31", "year", "9.9", None, [], "2020-01-01")

    assert out.status == VINTAGE_UNAVAILABLE


def test_point_written_after_as_of_is_absent_not_unavailable():
    """首次写入晚于请求时点 → 「那时还不知道」，与「无历史」是两件事。"""
    out = replay_point("2020-12-31", "year", "5.2", t(2022, 3, 1), [], "2021-01-01")

    assert out.status == ABSENT
    assert out.value is None


# --- 四态可区分 -------------------------------------------------------------

def test_the_four_states_are_distinct():
    """同一批输入里四种状态各出现一次，且互不混同。"""
    results = [
        replay_point("2020-12-31", "year", "1", t(2021, 1, 1), [], "2022-01-01"),        # exact
        replay_point("2021-06-15", "day", "2", None,
                     [Revision(t(2024, 5, 1), None, None, "batch", "b",
                               "2020-01-01", "2024-04-30", 1)], "2024-06-01"),           # batch
        replay_point("2019-12-31", "year", "3", None,
                     [Revision(t(2024, 6, 1), None, "9", revision_id=2)], "2020-01-01"),  # unavailable
        replay_point("2020-12-31", "year", "4", t(2022, 3, 1), [], "2021-01-01"),        # absent
    ]
    statuses = [r.status for r in results]

    assert statuses == [EXACT, BATCH_UNREPLAYABLE, VINTAGE_UNAVAILABLE, ABSENT]
    summary = summarize(results)
    assert summary["points"] == 4
    assert summary["fully_exact"] is False


def test_summary_fully_exact_only_when_every_point_exact():
    results = [replay_point("2020-12-31", "year", "1", t(2021, 1, 1), [], "2022-01-01")]

    summary = summarize(results)

    assert summary["fully_exact"] is True
    assert summary["exact"] == 1


# --- as_of 解析 -------------------------------------------------------------

def test_bare_date_means_end_of_day():
    """裸日期按当日结束：当天写入的修订应当可见。"""
    revs = [Revision(revised_at=t(2024, 6, 1, 18), old_value="a", new_value="b", revision_id=1)]

    out = replay_point("2023-12-31", "year", "b", None, revs, "2024-06-01")

    assert out.value == "b"


def test_iso_datetime_with_timezone_is_normalized():
    assert normalize_as_of("2024-06-01T00:00:00Z") == datetime(2024, 6, 1, 0, 0)


def test_invalid_as_of_rejected():
    with pytest.raises(ValueError):
        normalize_as_of("not-a-date")
