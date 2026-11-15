"""限流退避与中止的单测（无 DB、无网络）。

背景（2026-11-12 实测）：`astock-daily-increment` 连续三天失败，线上只留
"pod 已回收，无终态可抓"。进容器看现场才发现两类原因叠着：分片库在 recovery，
以及 eastmoney 对单 IP 限流后脚本仍在**硬打**——3 次重试之间没有 sleep，
一秒内就把 000525/000608/000609/000610 各打 3 次。akshare 自己的报错文案写着
"反复触发后可能需要 40 分钟以上"，所以硬打正是把 5 分钟封禁拖长的原因。

三条用例各自能单独跑红：
1. 限流时**立刻换 provider、不原地等**（全被拒才抛），且不把它记成"该符号没数据"；
2. 非限流的普通错误只做短指数退避（2/4 秒，每个 provider 各一轮），不套用限流窗口；
3. 连续 N 个符号被限流时**中止整轮**，剩下的符号一次都不请求（且跨分片不清零）。

**2026-11-15 订正**：第 1、2 条原来写的是「限流必须等 RATE_LIMIT_BACKOFF_S=300 秒」，
那是**单 provider 时代**的语义。2026-11-09 eastmoney 被封之后 `fetch_frame` 改成多
provider 故障转移（`PROVIDER_ORDER`），一条腿被限流就立刻换下一条——原地等 5 分钟
的前提「没有别的腿可用」已经不成立了，那个常量也成了死常量（已删）。断言现在按
**当前设计**写：限流零 sleep、全拒才抛；普通错误每个 provider 退避 RETRIES-1 次。
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

    两个细节是必须的，缺一个这两条用例就红（2026-11-15 实测）：
    ① **假模块要带齐 `_provider_fn` 一次取的三条导出**（`stock_zh_a_hist` /
       `stock_zh_a_hist_tx` / `stock_zh_a_daily`）。它是一张表一次建好的，少一条
       就在**取函数那一步**抛 AttributeError——而那是参数求值，仍落在 `try` 里，
       于是被当成「普通错误」走了指数退避，限流文案再也没机会被看到。
    ② **`_PROVIDER_FNS` 是模块级缓存**，而本文件的 `mod` 是 module 作用域的：
       前一条用例建过的表会跨用例留下来，之后换假模块就不生效。必须置 None 重建。
    """
    import sys
    import types

    def boom(*a, **kw):
        raise RuntimeError(message)

    fake = types.ModuleType("akshare")
    for name in ("stock_zh_a_hist", "stock_zh_a_hist_tx", "stock_zh_a_daily"):
        setattr(fake, name, boom)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(mod, "_PROVIDER_FNS", None)
    monkeypatch.setattr(mod, "_fetch_with_alarm", boom)


RATE_LIMIT_TEXT = ("东方财富 push2 行情服务器断开了连接，通常是短时间内请求过多，"
                   "当前 IP 被临时限流。首次限流一般约 5 分钟后恢复。")


def test_rate_limited_fails_over_without_waiting_then_raises(mod, sleeps, monkeypatch):
    """限流**不原地等**：还有 provider 就立刻换腿，全被拒才抛 RateLimited。

    2026-11-09 之前是「等 300 秒再试同一条腿」（那时只有一条腿）。现在三条腿轮换，
    等 5 分钟纯属浪费——eastmoney 被封而 tencent/sina 正常时，等待会让整轮崩在
    13 分钟处，正是当初连挂三天的形态。
    """
    _always_raise(mod, monkeypatch, RATE_LIMIT_TEXT)
    failed: list[str] = []
    with pytest.raises(mod.RateLimited) as ei:
        mod.fetch_frame("000525", "hfq", "2026-11-01", "2026-11-12", failed)
    assert sleeps == [], f"限流时不许 sleep，应立即试下一条腿（实际 {sleeps}）"
    # 限流不是"该符号没数据"：记进 failed 会把它伪装成缺口
    assert failed == [], failed
    # 抛出来的错误要点名所有被拒的腿，否则现场分不清「全拒」与「只有一条被拒」
    msg = str(ei.value)
    for name in mod.PROVIDER_ORDER:
        assert name in msg, f"RateLimited 文案没点名 {name}: {msg}"


def test_ordinary_error_uses_short_backoff_not_the_rate_limit_wait(mod, sleeps, monkeypatch):
    """普通错误只做短指数退避，且**每个 provider 各自一轮**（故障转移的代价）。"""
    _always_raise(mod, monkeypatch, "ValueError: unexpected payload")
    failed: list[str] = []
    assert mod.fetch_frame("600000", "hfq", "2026-11-01", "2026-11-12", failed) is None
    per_provider = [2 ** i for i in range(1, mod.RETRIES)]
    assert sleeps == per_provider * len(mod.PROVIDER_ORDER), sleeps
    # 量级判据：短退避最长 2**(RETRIES-1) = 4 秒，与 provider 自己acknowledged 的
    # 「限流约 5 分钟恢复」不是一个量级；这里不引常量，免得删常量就删掉这条断言。
    assert max(sleeps) <= 2 ** (mod.RETRIES - 1), sleeps
    assert failed == ["600000:hfq:RuntimeError"], failed


def test_all_breakers_open_still_raises_so_the_run_stops(mod):
    """三条腿全被熔断打开时必须照样抛 RateLimited。

    熔断（跳过 provider）与中止（连续 N 次 RateLimited 就停轮）原先是两套不咬合的
    机制：三条腿都被打开之后，`fetch_frame` 一次都不请求、既不抛也不记 `failed`，
    直接 `return None` ⇒ `run_symbols` 的连续计数永远是 0，整轮 4,576 个符号
    一个不取、`failed` 空、退出码 0，看上去一切正常。2026-11-15 实测该路径返回 None。
    """
    mod._reset_provider_state()
    mod._PROVIDER_STATE["open"].update(mod.PROVIDER_ORDER)
    failed: list[str] = []
    try:
        with pytest.raises(mod.RateLimited) as ei:
            mod.fetch_frame("600000", "qfq", "2026-11-01", "2026-11-12", failed)
        assert failed == [], "熔断不是「该符号没数据」，不许进 failed"
        assert "breaker open" in str(ei.value), str(ei.value)
    finally:
        mod._reset_provider_state()   # mod 是 module 作用域：别把 open 泄给后面的用例


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
