"""腾讯直连取数的两条红线（无网络、无 DB）。

背景（2026-11-26 实测）：2659 从 11-16 起每个交易日都被 180min deadline 杀掉。
根因不是上游没数据，是取数慢：akshare 的 `stock_zh_a_hist_tx` 用自带纯 Python
`demjson` 解码，同一份 56KB 响应 **3.23s** vs `json.loads` **0.002s**（1597 倍），
外加逐行 `.apply(pd.to_datetime)` **3.09s**/640 行。合计 7.4s/符号 × 5203 = 10.6h。

两条用例各自能单独跑红：
1. 解码必须是 json —— 把 `_json.loads` 换回慢解码器，这条会红；
2. 日期解析必须向量化 —— 改回 row-wise apply，这条会红。
两条都只断言**形状与取值**，不断言耗时（耗时在上游机器上不稳定，断言它会 flaky）；
速度靠注释里记的那次实测与 bench 脚本，不靠 CI。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bulk_ingest_astock_daily.py"

# What the endpoint really returns (measured 2026-11-26): TEN fields, and index 6 is
# a dict — not a number. akshare keeps [0,1,2,3,4,5,7,8], so 7 is turnover and 8 is
# amount. A fixture with nine fields silently shifts those two columns.
FAKE_BODY = (
    'kline_dayhfq2026={"code":0,"data":{"sh600036":{"hfqday":['
    '["2026-11-24","40.73","41.11","41.16","40.60","11942400",{},"0.49","771751.30",""],'
    '["2026-11-25","41.10","41.32","41.40","41.00","12000000",{},"0.50","780000.00",""]'
    "]}}}"
)


def _load():
    spec = importlib.util.spec_from_file_location("astock_daily_tx", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture
def fake_net(mod, monkeypatch):
    """Serve FAKE_BODY without touching the network; record how often it was asked."""
    calls: list[dict] = []

    class _Resp:
        text = FAKE_BODY

    class _Requests:
        @staticmethod
        def get(url, params=None, timeout=None):
            calls.append(dict(params or {}))
            return _Resp()

    mod._TX_LIBS.clear()
    mod._TX_LIBS.update(json=__import__("json"), pd=pytest.importorskip("pandas"), requests=_Requests)
    return calls


def test_tx_direct_uses_json_decoder_and_returns_akshare_shape(mod, fake_net):
    """Decoded via json, and the frame carries the column names the writer expects."""
    df = mod._tencent_direct("sh600036", "20261101", "20261130", "hfq")

    assert list(df.columns)[:6] == ["date", "open", "close", "high", "low", "volume"]
    assert len(df) == 2
    # This stage mirrors akshare: volume is in 股 (main-board 手 × 100). The ÷100
    # back to 手 happens downstream in _normalize_frame, which the writer needs.
    assert float(df["volume"].iloc[0]) == pytest.approx(11942400 * 100)
    assert float(df["turnover"].iloc[0]) == pytest.approx(0.49 / 100)
    assert float(df["amount"].iloc[0]) == pytest.approx(771751.30 * 10000)
    assert df["close"].iloc[0] == pytest.approx(41.11)
    # dates land as date objects, sorted, one per day
    assert str(df["date"].iloc[0]) == "2026-11-24"
    assert str(df["date"].iloc[1]) == "2026-11-25"


def test_tx_direct_filters_to_the_requested_window(mod, fake_net):
    """A narrow window must not require a second round trip and must not leak rows."""
    df = mod._tencent_direct("sh600036", "20261125", "20261126", "hfq")

    assert len(df) == 1, "only the in-window day is returned"
    assert str(df["date"].iloc[0]) == "2026-11-25"
    # one request for a one-year window (the year loop must not over-ask)
    assert len(fake_net) == 1, fake_net


def test_tx_direct_parses_dates_without_row_wise_apply(mod, fake_net, monkeypatch):
    """Row-wise `.apply(pd.to_datetime)` is 3.09s/640 rows and produces the SAME
    values as the vectorised parse — so a value assertion cannot catch it going
    back. It is 88% of the fetch and the reason 2659 missed its deadline, so the
    regression is caught by forbidding the call, not by timing it (timing would
    be flaky on a loaded runner)."""
    pd = mod._tx_libs()["pd"]
    real_apply = pd.DataFrame.apply

    def guarded_apply(self, *a, **kw):
        raise AssertionError("row-wise DataFrame.apply is back in the date parse")

    monkeypatch.setattr(pd.DataFrame, "apply", guarded_apply)
    try:
        df = mod._tencent_direct("sh600036", "20261101", "20261130", "hfq")
        assert len(df) == 2, "vectorised path must still return the rows"
    finally:
        monkeypatch.setattr(pd.DataFrame, "apply", real_apply)


def test_tx_libs_are_cached_not_reimported(mod):
    """"Pay pandas' first import once" is the whole point of the handle cache."""
    first = mod._tx_libs()
    assert mod._tx_libs() is first, "handles must be cached, not rebuilt per call"