"""Panel i18n helpers (panel-rbac-i18n-refresh, task 4.1).

Pure utility module — no FastAPI/Jinja imports — so app.py wires these into
templates as globals/filters in a later batch and unit tests stay light.
zh ⇄ en only: zh is the source of truth (templates keep writing Chinese),
``EN`` maps 中文原文 → English. Display timezone is fixed Asia/Shanghai
(same convention as visibility/digest.py's ``SCRAW_DIGEST_TZ`` default).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

LANG_COOKIE = "panel-lang"
LANGUAGES = ("zh", "en")
DEFAULT_LANG = "zh"

_TZ = ZoneInfo("Asia/Shanghai")

# 核心词条（导航 + 通用按钮 + 状态词）。键=面板中文原文，值=英文；
# 后续模板批次按需补充，miss 时 translate 回退中文原文。
EN: dict[str, str] = {
    # 导航 navigation
    "首页": "Home",
    "控制台": "Console",
    "策略": "Policies",
    "运行": "Runs",
    "源": "Sources",
    "漏斗": "Funnel",
    "认证": "Auth",
    "代理": "Proxies",
    "数据": "Data",
    "指标": "Indicators",
    "看板": "Dashboard",
    "任务": "Tasks",
    "日志": "Logs",
    "设置": "Settings",
    "概览": "Overview",
    "详情": "Detail",
    # 通用按钮 common buttons
    "登录": "Sign in",
    "退出": "Sign out",
    "退出登录": "Sign out",
    "启用": "Enable",
    "停用": "Disable",
    "删除": "Delete",
    "保存": "Save",
    "取消": "Cancel",
    "确认": "Confirm",
    "编辑": "Edit",
    "新增": "Add",
    "新建": "Create",
    "触发": "Trigger",
    "刷新": "Refresh",
    "搜索": "Search",
    "重置": "Reset",
    "返回": "Back",
    "上一页": "Previous",
    "下一页": "Next",
    "提交": "Submit",
    "导出": "Export",
    "查看": "View",
    "操作": "Actions",
    "状态": "Status",
    # 状态词 status words
    "暂无数据": "No data",
    "运行中": "Running",
    "成功": "Success",
    "失败": "Failed",
    "超时": "Timeout",
    "等待中": "Pending",
    "已启用": "Enabled",
    "已停用": "Disabled",
    "总计": "Total",
    "全部": "All",
    "名称": "Name",
    "时间": "Time",
    "类型": "Type",
    "备注": "Note",
}

# 分组词条装载（task 4.3）：模板批次各自维护 ``i18n_en_<group>.py``（同目录，
# 定义一个 ``EN_ENTRIES: dict[str, str]``），此处统一合并——并行批次互不写同一
# 文件，装载顺序无关（后者覆盖前者仅限同键，视为词条修正）。
try:  # pragma: no cover - trivial loader, exercised by every group file
    for _mod in sorted(Path(__file__).parent.glob("i18n_en_*.py")):
        import importlib.util as _ilu

        _spec = _ilu.spec_from_file_location(_mod.stem, _mod)
        _m = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_m)
        EN.update(getattr(_m, "EN_ENTRIES", {}))
    del _mod, _spec, _m, _ilu  # type: ignore[misc]
except NameError:
    pass  # no group files exist yet (glob found nothing → _mod unbound)


def resolve_locale(cookie_value: str | None,
                   accept_language: str | None) -> str:
    """Pick the panel locale: a valid cookie wins; else the first zh*/en*
    tag in Accept-Language (q 值忽略——浏览器发送顺序即优先级);
    otherwise zh. None-safe on both inputs."""
    if cookie_value in LANGUAGES:
        return cookie_value
    for tag in (accept_language or "").split(","):
        tag = tag.split(";")[0].strip().lower()
        if tag.startswith("zh"):
            return "zh"
        if tag.startswith("en"):
            return "en"
    return DEFAULT_LANG


def translate(s: str, locale: str) -> str:
    """zh（及任何非 en locale）恒等返回；en 查 ``EN``，miss 回退中文原文。"""
    if locale != "en" or not s:
        return s
    return EN.get(s, s)


def _trim1(v: float) -> str:
    """One decimal max, trailing '.0' trimmed: 404.9→'404.9', 1.0→'1'."""
    s = f"{v:.1f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt_int(n: int | None, locale: str) -> str:
    """Format an integer for display; None → "" (blank cell).

    zh: |n|≥1e8 → "N.N 亿"(120000000→"1.2 亿"); |n|≥1e4 → "N.N 万"
        (4049000→"404.9 万"); scaled values keep ≤1 decimal with ".0"
        trimmed; plain values use thousands commas (9876→"9,876").
    en: |n|≥1e6 → "N.NM"(4049000→"4.0M"); |n|≥1e3 → "N.Nk"
        (12500→"12.5k"); scaled values keep exactly 1 decimal;
        plain values use thousands commas.
    Negatives go through the same magnitude rules with a "-" prefix.
    """
    if n is None:
        return ""
    n = int(n)
    sign = "-" if n < 0 else ""
    a = abs(n)
    if locale != "en":
        if a >= 100_000_000:
            return f"{sign}{_trim1(a / 100_000_000)} 亿"
        if a >= 10_000:
            return f"{sign}{_trim1(a / 10_000)} 万"
    elif a >= 1_000_000:
        return f"{sign}{a / 1_000_000:.1f}M"
    elif a >= 1_000:
        return f"{sign}{a / 1_000:.1f}k"
    return f"{n:,}"


def _to_utc(value: dt.datetime | str | None) -> dt.datetime | None:
    """Normalize datetime/ISO-string to aware-UTC; naive 视作 UTC;
    unparsable → None."""
    if isinstance(value, dt.datetime):
        ts = value
    elif isinstance(value, str) and value.strip():
        s = value.strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"  # py3.10 fromisoformat has no "Z" support
        try:
            ts = dt.datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc)


def fmt_ts(value: dt.datetime | str | None, locale: str,
           now: dt.datetime | str | None = None) -> str:
    """Timestamp → "YYYY-MM-DD HH:MM:SS" in Asia/Shanghai, second precision
    (no microseconds, no offset suffix). Accepts datetime or ISO string
    (naive = UTC; "Z"/"+00:00"/microseconds handled). Relative hint appended
    when the moment is <24h old: <1min → "（刚刚）"/" (just now)";
    <24h → "（N 小时前）"/" (Nh ago)"; otherwise absolute-only.
    ``now`` (aware, or naive = UTC) lets tests pin the hint. None → ""."""
    ts = _to_utc(value)
    if ts is None:
        return ""
    stamp = ts.astimezone(_TZ).strftime("%Y-%m-%d %H:%M:%S")
    ref = now if now is not None else dt.datetime.now(dt.timezone.utc)
    ref = _to_utc(ref)
    if ref is None:
        return stamp
    secs = (ref - ts).total_seconds()
    zh = locale != "en"
    if secs < 60:  # includes near-future timestamps
        return f"{stamp}（刚刚）" if zh else f"{stamp} (just now)"
    if secs < 24 * 3600:
        hours = int(secs // 3600)
        return (f"{stamp}（{hours} 小时前）" if zh
                else f"{stamp} ({hours}h ago)")
    return stamp


_DOW_ZH = ("日", "一", "二", "三", "四", "五", "六")  # 拼 "每周{X}"
_DOW_EN = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def cron_humanize(expr: str | None, locale: str) -> str | None:
    """5 段标准 cron（分 时 日 月 周）→ 面板可读文案；未识别形态返回
    None，调用方回退显示原表达式。分钟与小时补零到两位；日不补零。

    "23 3 * * *"   → "每天 03:23" / "daily 03:23"
    "23 3 * * 3"   → "每周三 03:23" / "weekly Wed 03:23"（0/7 均为周日）
    "23 3 5 * *"   → "每月 5 日 03:23" / "monthly day 5, 03:23"
    "0 */6 * * *"  → "每 6 小时" / "every 6h"（仅整点步进形态）
    """
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    minute, hour, dom, mon, dow = parts
    if mon != "*":
        return None
    zh = locale != "en"

    # 每 N 小时：整点分钟 + 小时步进，其余域通配
    if minute == "0" and dom == "*" and dow == "*" and hour.startswith("*/"):
        try:
            step = int(hour[2:])
        except ValueError:
            return None
        if step > 0:
            return f"每 {step} 小时" if zh else f"every {step}h"
        return None

    # 以下形态需要字面量分钟/小时
    if not (minute.isdigit() and hour.isdigit()):
        return None
    m, h = int(minute), int(hour)
    if not (0 <= m <= 59 and 0 <= h <= 23):
        return None
    hm = f"{h:02d}:{m:02d}"

    if dom == "*" and dow == "*":
        return f"每天 {hm}" if zh else f"daily {hm}"
    if dom == "*" and dow.isdigit() and int(dow) <= 7:
        idx = int(dow) % 7  # 0 与 7 都是周日
        return (f"每周{_DOW_ZH[idx]} {hm}" if zh
                else f"weekly {_DOW_EN[idx]} {hm}")
    if dow == "*" and dom.isdigit() and 1 <= int(dom) <= 31:
        return (f"每月 {int(dom)} 日 {hm}" if zh
                else f"monthly day {int(dom)}, {hm}")
    return None


def next_fire(expr: str | None,
              now: dt.datetime | None = None) -> dt.datetime | None:
    """下次触发时刻，aware Asia/Shanghai datetime；非法/未支持表达式
    返回 None。croniter 为既有依赖（pyproject: croniter>=2.0，reconciler/
    station_ops 同款用法：base 转面板时区后 get_next）。naive ``now``
    视作上海墙钟时间。"""
    if not expr:
        return None
    base = now if now is not None else dt.datetime.now(tz=_TZ)
    if base.tzinfo is None:
        base = base.replace(tzinfo=_TZ)
    try:
        return croniter(expr, base.astimezone(_TZ)).get_next(dt.datetime)
    except Exception:  # noqa: BLE001 - bad expr → caller shows raw text
        return None
