"""读路径写入必须用概念自己的频率标 granularity，不能用默认 `day`。

审计 04 §2：去重键是 `(date, granularity)`。爬虫按概念真实频率标（月度 → "month"），
而读路径原先不传 granularity，落默认 "day"。于是同一个经济点会有两行：

* 爬虫行 `(date="2026-10-01", granularity="month")`，过了 25 天 TTL 变陈旧；
* 下一次 `read()` 取到新值，写成 `(date="2026-10-01", granularity="day")`。

`_dedupe_range` 按 `(date, granularity)` 去重，两行都留下 → `read_series` 同一
日期返回两次，求和/连乘双计；点读 `read_cache` 又不看 granularity，按 source
rank 与行 id 任选一行——选谁不看频率。

两处写点都在 `fetch/dispatch.py`：点读 `:256` 与区间读 `:551`。修法是按
`concept.frequency` 推出同一个标签。这里刻意不 import `crawl/planner`（那是
crawl 层，fetch 层反向依赖会把整个调度拉进读路径），所以用
`tests/test_write_granularity.py` 把两边同义这件事钉住——将来谁改了一边，这里红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_open_data_mcp.fetch.dispatch import _write_granularity  # noqa: E402


@pytest.mark.parametrize(
    "frequency,expected",
    [
        ("yearly", "year"),
        ("annual", "day"),      # 未识别的频率保守落 day（与 planner 同义）
        ("monthly", "month"),
        ("quarterly", "day"),
        ("daily", "day"),
        ("weekly", "day"),
        (None, "day"),
        ("unknown", "day"),
    ],
)
def test_frequency_maps_to_granularity(frequency, expected):
    assert _write_granularity(frequency) == expected


def test_helper_matches_planner_definition():
    """两侧必须同义：读路径与爬虫路径用不同标签会把同一点写成两行。

    planner 属于 crawl 层，import 它有环依赖风险，所以这里退一步：把两个实现的
    判定规则都跑一遍同样的输入，要求结果逐一相同。planner 若改了规则，本用例红。
    """
    from fd_open_data_mcp.crawl.planner import _granularity_for

    for freq in ("yearly", "annual", "monthly", "quarterly", "daily", "weekly", None, "unknown"):
        assert _write_granularity(freq) == _granularity_for(freq), (
            f"频率 {freq!r} 上两个实现不一致：读路径 {_write_granularity(freq)!r} "
            f"vs planner {_granularity_for(freq)!r}"
        )


def test_both_write_sites_pass_granularity():
    """两处写点都必须显式传 granularity，否则默认值会悄悄回来。"""
    import inspect

    from fd_open_data_mcp.fetch import dispatch as mod

    src = inspect.getsource(mod)
    assert src.count("granularity=_write_granularity(concept.frequency)") >= 2, (
        "点读与区间读两处都应传 granularity；少一处就还会落出 day 行"
    )
