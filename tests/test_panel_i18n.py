"""Unit tests for panel i18n helpers (panel-rbac-i18n-refresh, task 4.1).

纯单元测试——不 import app，不碰模板/DB。"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from fd_open_data_mcp.panel import i18n

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = dt.timezone.utc


# ── resolve_locale ───────────────────────────────────────────────────────────
def test_resolve_locale_cookie_wins():
    assert i18n.resolve_locale("en", "zh-CN,zh;q=0.9") == "en"
    assert i18n.resolve_locale("zh", "en-US,en;q=0.9") == "zh"


def test_resolve_locale_invalid_cookie_falls_through_to_header():
    assert i18n.resolve_locale("fr", "en-GB,en;q=0.8") == "en"
    assert i18n.resolve_locale("", "zh-CN") == "zh"


def test_resolve_locale_accept_language_first_zh_en_prefix():
    assert i18n.resolve_locale(None, "zh-CN,zh;q=0.9,en;q=0.8") == "zh"
    assert i18n.resolve_locale(None, "en-US,en;q=0.9") == "en"
    # q 值忽略：按出现顺序取首个 zh*/en*（fr 跳过）
    assert i18n.resolve_locale(None, "fr-FR,fr;q=0.9,en-US;q=0.8") == "en"


def test_resolve_locale_defaults_to_zh():
    assert i18n.resolve_locale(None, None) == "zh"
    assert i18n.resolve_locale(None, "") == "zh"
    assert i18n.resolve_locale(None, "fr-FR,fr;q=0.9,de;q=0.8") == "zh"
    assert i18n.resolve_locale("junk", "junk") == "zh"


# ── translate ────────────────────────────────────────────────────────────────
def test_translate_zh_identity():
    assert i18n.translate("首页", "zh") == "首页"
    assert i18n.translate("字典里没有的句子", "zh") == "字典里没有的句子"


def test_translate_en_hit():
    assert i18n.translate("首页", "en") == "Home"
    assert i18n.translate("策略", "en") == "Policies"
    assert i18n.translate("暂无数据", "en") == "No data"


def test_translate_en_miss_falls_back_to_source():
    assert i18n.translate("字典里没有的句子", "en") == "字典里没有的句子"


# ── fmt_int ──────────────────────────────────────────────────────────────────
def test_fmt_int_zh_wan_yi_and_plain():
    assert i18n.fmt_int(4_049_000, "zh") == "404.9 万"
    assert i18n.fmt_int(120_000_000, "zh") == "1.2 亿"
    assert i18n.fmt_int(9_876, "zh") == "9,876"


def test_fmt_int_en_m_and_k():
    assert i18n.fmt_int(4_049_000, "en") == "4.0M"
    assert i18n.fmt_int(12_500, "en") == "12.5k"
    assert i18n.fmt_int(9_876, "en") == "9.9k"  # ≥1e3 即走 k
    assert i18n.fmt_int(876, "en") == "876"


def test_fmt_int_negative_and_none_safe():
    assert i18n.fmt_int(-12_500, "en") == "-12.5k"
    assert i18n.fmt_int(-100_000, "zh") == "-10 万"
    assert i18n.fmt_int(None, "zh") == ""
    assert i18n.fmt_int(None, "en") == ""


# ── fmt_ts ───────────────────────────────────────────────────────────────────
def test_fmt_ts_shanghai_seconds_precision_and_relative():
    iso = "2026-10-07T05:29:09.000946+00:00"
    now = dt.datetime(2026, 10, 7, 8, 29, 30, tzinfo=UTC)  # 3 小时 21 秒后
    assert i18n.fmt_ts(iso, "zh", now=now) == "2026-10-07 13:29:09（3 小时前）"
    assert i18n.fmt_ts(iso, "en", now=now) == "2026-10-07 13:29:09 (3h ago)"


def test_fmt_ts_just_now_and_older_than_24h():
    iso = "2026-10-07T05:29:09+00:00"
    now = dt.datetime(2026, 10, 7, 5, 29, 45, tzinfo=UTC)  # 36 秒后
    assert "刚刚" in i18n.fmt_ts(iso, "zh", now=now)
    assert "just now" in i18n.fmt_ts(iso, "en", now=now)
    # >24h：只留绝对时间
    old = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
    assert i18n.fmt_ts(iso, "zh", now=old) == "2026-10-07 13:29:09"


def test_fmt_ts_naive_inputs_treated_as_utc():
    now = dt.datetime(2026, 10, 7, 5, 29, 30, tzinfo=UTC)
    out = i18n.fmt_ts(dt.datetime(2026, 10, 7, 5, 29, 0), "en", now=now)
    assert out == "2026-10-07 13:29:00 (just now)"


def test_fmt_ts_none_returns_empty():
    assert i18n.fmt_ts(None, "zh") == ""
    assert i18n.fmt_ts("not-a-timestamp", "en") == ""


# ── cron_humanize ────────────────────────────────────────────────────────────
def test_cron_humanize_daily():
    assert i18n.cron_humanize("23 3 * * *", "zh") == "每天 03:23"
    assert i18n.cron_humanize("23 3 * * *", "en") == "daily 03:23"


def test_cron_humanize_weekly_and_monthly():
    assert i18n.cron_humanize("23 3 * * 3", "zh") == "每周三 03:23"
    assert i18n.cron_humanize("23 3 * * 3", "en") == "weekly Wed 03:23"
    assert i18n.cron_humanize("23 3 * * 0", "zh") == "每周日 03:23"
    assert i18n.cron_humanize("23 3 5 * *", "zh") == "每月 5 日 03:23"
    assert i18n.cron_humanize("23 3 5 * *", "en") == "monthly day 5, 03:23"


def test_cron_humanize_hour_interval():
    assert i18n.cron_humanize("0 */6 * * *", "zh") == "每 6 小时"
    assert i18n.cron_humanize("0 */6 * * *", "en") == "every 6h"


def test_cron_humanize_unsupported_returns_none():
    # 分钟步进（秒级式样）按未识别处理
    assert i18n.cron_humanize("*/7 * * * *", "zh") is None
    assert i18n.cron_humanize("*/7 * * * *", "en") is None
    assert i18n.cron_humanize("not-a-cron", "zh") is None
    assert i18n.cron_humanize(None, "en") is None
    assert i18n.cron_humanize("23 3 * 10 *", "zh") is None  # 非通配月未支持


# ── next_fire ────────────────────────────────────────────────────────────────
def test_next_fire_daily_next_day_in_shanghai():
    now = dt.datetime(2026, 10, 7, 12, 0, 0, tzinfo=SHANGHAI)
    nxt = i18n.next_fire("23 3 * * *", now=now)
    assert nxt is not None
    assert (nxt.year, nxt.month, nxt.day) == (2026, 10, 8)
    assert (nxt.hour, nxt.minute, nxt.second) == (3, 23, 0)
    assert nxt.tzinfo is not None
    assert nxt.utcoffset() == dt.timedelta(hours=8)  # Asia/Shanghai


def test_next_fire_invalid_or_empty_returns_none():
    now = dt.datetime(2026, 10, 7, tzinfo=UTC)
    assert i18n.next_fire("99 bad * * *", now=now) is None
    assert i18n.next_fire("", now=now) is None
    assert i18n.next_fire(None, now=now) is None
