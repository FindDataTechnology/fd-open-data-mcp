"""限流退避与中止的单测（无 DB、无网络）。

背景（2026-11-12 实测）：`astock-daily-increment` 连续三天失败，线上只留
"pod 已回收，无终态可抓"。进容器看现场才发现两类原因叠着：分片库在 recovery，
以及 eastmoney 对单 IP 限流后脚本仍在**硬打**——3 次重试之间没有 sleep，
一秒内就把 000525/000608/000609/000610 各打 3 次。akshare 自己的报错文案写着
"反复触发后可能需要 40 分钟以上"，所以硬打正是把 5 分钟封禁拖长的原因。

三条用例各自能单独跑红：
1. 限流时**必须等 RATE_LIMIT_BACKOFF_S 秒**再重试，且不把它记成"该符号没数据"；
2. 非限流的普通错误只做指数退避，不套用 5 分钟档；
3. 连续 N 个符号被限流时**中止整轮**，剩下的符号一次都不请求（且跨分片不清零）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bulk_ingest_astock_daily.py"


def _load():
    spec = importlib.util.spec_from_file_location("astock_daily_rl", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture
def sleeps(mod, monkeypatch):
    """记录 sleep 而不是真的睡——"有没有等"正是要断言的东西。"""
    seen: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: seen.append(s))
    return seen


def _always_raise(mod, monkeypatch, message: str):
    """让取数每次都抛指定错误。

    `fetch_frame` 内部 `import akshare as ak`（akshare 只在爬虫镜像里装），
    所以这里注入一个假 akshare 模块，再把 `_fetch_with_alarm` 换成直接抛错——
    两层都打桩，测试既不依赖 akshare 也不发网络请求。
    """
    import sys
    import types

    def boom(*a, **kw):
        raise RuntimeError(message)

    fake = types.ModuleType("akshare")
    fake.stock_zh_a_hist = boom
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(mod, "_fetch_with_alarm", boom)


RATE_LIMIT_TEXT = ("东方财富 push2 行情服务器断开了连接，通常是短时间内请求过多，"
                   "当前 IP 被临时限流。首次限流一般约 5 分钟后恢复。")


def test_rate_limited_sleeps_the_full_window_then_raises(mod, sleeps, monkeypatch):
    _always_raise(mod, monkeypatch, RATE_LIMIT_TEXT)
    failed: list[str] = []
    with pytest.raises(mod.RateLimited):
        mod.fetch_frame("000525", "hfq", "2026-11-01", "2026-11-12", failed)
    assert sleeps == [mod.RATE_LIMIT_BACKOFF_S] * (mod.RETRIES - 1), sleeps
    # 限流不是"该符号没数据"：记进 failed 会把它伪装成缺口
    assert failed == [], failed


def test_ordinary_error_uses_short_backoff_not_the_rate_limit_wait(mod, sleeps, monkeypatch):
    _always_raise(mod, monkeypatch, "ValueError: unexpected payload")
    failed: list[str] = []
    assert mod.fetch_frame("600000", "hfq", "2026-11-01", "2026-11-12", failed) is None
    assert sleeps == [2 ** i for i in range(1, mod.RETRIES)], sleeps
    assert all(s < mod.RATE_LIMIT_BACKOFF_S for s in sleeps)
    assert failed == ["600000:hfq:RuntimeError"], failed


def test_consecutive_rate_limits_stop_the_run(mod):
    """三个符号连续被限流 → 立即返回 aborted，后续符号一次都不请求。"""
    codes = [f"6000{i:02d}" for i in range(10)]
    calls: list[str] = []

    def ingest(code):
        calls.append(code)
        raise mod.RateLimited(f"{code}: 限流")

    aborted, streak, processed = mod.run_symbols(codes, ingest)
    assert aborted is True
    assert processed == mod.RATE_LIMIT_STREAK_ABORT
    assert calls == codes[: mod.RATE_LIMIT_STREAK_ABORT], calls
    assert streak == mod.RATE_LIMIT_STREAK_ABORT


def test_a_success_resets_the_streak(mod):
    """限流计数必须是**连续**的：中间成功一个就清零，否则长跑会被误判成封禁。"""
    codes = [f"6000{i:02d}" for i in range(10)]
    state = {"n": 0}

    def ingest(code):
        state["n"] += 1
        # 限流、限流、成功、限流、限流、成功……永远不到三个连续
        if state["n"] % 3 == 0:
            return
        raise mod.RateLimited(f"{code}: 限流")

    aborted, streak, processed = mod.run_symbols(codes, ingest)
    assert aborted is False
    assert processed == len(codes)
    assert streak < mod.RATE_LIMIT_STREAK_ABORT


def test_streak_carries_across_slices(mod):
    """分片交界处不能把计数清零：冷却 sleep 不解除封禁。"""
    def ingest(code):
        raise mod.RateLimited(f"{code}: 限流")

    aborted, streak, _ = mod.run_symbols(["600000"], ingest)          # 1 次
    assert (aborted, streak) == (False, 1)
    aborted, streak, processed = mod.run_symbols(["600001", "600002"], ingest, streak=streak)
    assert aborted is True, "跨分片第 3 次即应中止"
    assert streak == mod.RATE_LIMIT_STREAK_ABORT
    assert processed == 2
