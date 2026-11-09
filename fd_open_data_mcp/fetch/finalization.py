"""定型窗口：观测的某一期在什么时刻之后不再可修订。

背景
----
`fetch/cache.py` 原先用 ``_period_final()`` 判定定型，规则是「年粒度跨年即定型」。这条
规则对月度/日度基本合理，对**年度经济统计**是错的：官方年度核算的修订会跨年出现
（GDP 可修订两三年），跨年即定型会让修订永远进不来——而且因为 ``is_stale()`` 对已定型
行恒返回 False，"最新值"其实是"初值"，连告警都不会响。

本模块把定型窗口做成显式对象，窗口按粒度给默认值、可被概念级覆盖，并额外提供
``finalized_through()`` 供区间读做分区（已定型段走缓存，只对未定型段回源）。

与重抓决策的关系
----------------
定型边界回答"这一期还能不能改"，**不回答**"这一行该不该再去上游取一次"。后者是 TTL
与摄取策略的事。两件事必须分开判定：揉在一起时，放宽定型窗口会连带改变读路径的回源
行为，把一次口径修正变成一次上游流量事件。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional


@dataclass(frozen=True)
class FinalizeWindow:
    """一期的定型窗口。

    ``grace_days`` 是「该期走完之后，还容许修订多久」。周/月/日粒度通常为 0；
    年度统计需要跨年窗口，默认见 ``DEFAULT_GRACE_DAYS``。
    """

    grace_days: int = 0

    def __post_init__(self) -> None:
        if self.grace_days < 0:
            raise ValueError(f"grace_days must be >= 0, got {self.grace_days}")

    @property
    def grace(self) -> timedelta:
        return timedelta(days=self.grace_days)


#: 各粒度在「周期本身走完」之上追加的修订宽限（天）。
#:
#: 注意年度粒度的**基数不是周期末日**而是**次年末**：某年的统计值在次年整年内都
#: 仍可被修订，到第三年元旦才定型。这对应官方年度核算的实际节奏，也是本 change 要
#: 修的旧缺陷（旧实现是「跨年即定型」，修订永远进不来）。``grace_days`` 在此基数上
#: 再追加，用于个别源需要更长窗口的情形。
DEFAULT_GRACE_DAYS: dict[str, int] = {
    "day": 0,
    "daily": 0,
    "week": 0,
    "weekly": 0,
    "month": 0,
    "monthly": 0,
    "quarter": 0,
    "quarterly": 0,
    "year": 0,
    "yearly": 0,
    "annual": 0,
    "unknown": 0,
}


def _normalize_granularity(granularity: Optional[str]) -> str:
    g = (granularity or "day").strip().lower()
    return g if g in DEFAULT_GRACE_DAYS else "unknown"


def default_window(granularity: Optional[str]) -> FinalizeWindow:
    """该粒度的保守默认窗口。未知粒度按 ``unknown`` 处理（不宽限，等同日粒度）。"""
    return FinalizeWindow(DEFAULT_GRACE_DAYS[_normalize_granularity(granularity)])


def resolve_window(granularity: Optional[str], concept=None) -> FinalizeWindow:
    """解析某概念在给定粒度下应使用的窗口。

    概念级覆盖优先（``Concept.finalize_grace_days``，None 表示未声明）；未声明时回落到
    粒度默认。非法值（负数）视为声明错误并抛出——静默退化为默认会让"声明了却没生效"
    变成一个看不见的坑。
    """
    override = getattr(concept, "finalize_grace_days", None) if concept is not None else None
    if override is None:
        return default_window(granularity)
    if not isinstance(override, int) or isinstance(override, bool) or override < 0:
        raise ValueError(
            f"invalid finalize_grace_days={override!r} on concept "
            f"{getattr(concept, 'id', '?')}; expected int >= 0"
        )
    return FinalizeWindow(override)


def _parse_date(obs_date: Optional[str]) -> Optional[date]:
    """解析观测日期。接受 ``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD``。

    存库日期本该是规范 ``YYYY-MM-DD``，但历史上存在裸 ``YYYY`` 的年度行
    （``period_prefixes`` 的 docstring 已记这一点），所以这里按前缀宽松解析。
    """
    if not obs_date:
        return None
    raw = str(obs_date).strip()
    try:
        if len(raw) >= 10:
            return date.fromisoformat(raw[:10])
        if len(raw) == 7:
            return date.fromisoformat(raw + "-01")
        if len(raw) == 4:
            return date(int(raw), 12, 31)
    except ValueError:
        return None
    return None


def period_end(obs_date: Optional[str], granularity: Optional[str]) -> Optional[date]:
    """该观测所属周期的最后一天。无法解析时返回 None（调用方按"未定型"处理）。"""
    d = _parse_date(obs_date)
    if d is None:
        return None
    g = _normalize_granularity(granularity)
    if g in ("year", "yearly", "annual"):
        return date(d.year, 12, 31)
    if g in ("month", "monthly"):
        return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    if g in ("quarter", "quarterly"):
        q_end_month = ((d.month - 1) // 3) * 3 + 3
        return date(d.year, q_end_month, calendar.monthrange(d.year, q_end_month)[1])
    if g in ("week", "weekly"):
        return d + timedelta(days=6 - d.weekday())
    # day / unknown：周期就是这一天
    return d


def _today(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).date()


def finalize_limit(
    obs_date: Optional[str],
    granularity: Optional[str],
    window: Optional[FinalizeWindow] = None,
) -> Optional[date]:
    """该观测**仍可修订**的最后一天。这一天之后（严格大于）即为定型。

    按周期算术而非固定天数推导，避免闰年把边界推早一天：

    * 年度：基数为**次年 12-31**（次年整年可修订），再追加 ``grace_days``。
    * 月度/季度/周/日：基数为该周期末日，再追加 ``grace_days``——即"周期走完的
      下一天起定型"，与既有语义一致。
    """
    end = period_end(obs_date, granularity)
    if end is None:
        return None
    win = window if window is not None else default_window(granularity)
    g = _normalize_granularity(granularity)
    if g in ("year", "yearly", "annual"):
        base = date(end.year + 1, 12, 31)
    else:
        base = end
    return base + win.grace


def is_finalized(
    obs_date: Optional[str],
    granularity: Optional[str],
    window: Optional[FinalizeWindow] = None,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """这一期是否已过定型窗口（即不可再修订）。

    保守取向：日期无法解析、窗口非法时一律返回 False（当作未定型），而不是把不确定
    当成定型——把不能改的东西当成能改只是浪费一次写入，反过来会让修订被静默吞掉。
    """
    limit = finalize_limit(obs_date, granularity, window)
    if limit is None:
        return False
    return _today(now) > limit


def finalized_through(
    granularity: Optional[str],
    window: Optional[FinalizeWindow] = None,
    *,
    now: Optional[datetime] = None,
) -> date:
    """返回分区切点：**严格早于**该日期的观测都已定型。

    区间读用它把请求窗口切成两段——已定型段全部从缓存取，只对之后的部分回源。这样
    "尾部有陈旧行"不会把整段历史拖去上游重抓。

    年度粒度下"今年"与"去年"都未定型，所以切点是**前年**的 1 月 1 日。
    """
    win = window if window is not None else default_window(granularity)
    today = _today(now)
    g = _normalize_granularity(granularity)

    if g in ("year", "yearly", "annual"):
        # 年 Y 定型 <=> today > (Y+1)-12-31。故最后一个定型年是 today.year - 2。
        cutoff = date(today.year - 1, 1, 1) - win.grace
        return date(cutoff.year, 1, 1)
    if g in ("month", "monthly"):
        cutoff = date(today.year, today.month, 1) - win.grace
        return date(cutoff.year, cutoff.month, 1)
    if g in ("quarter", "quarterly"):
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        cutoff = date(today.year, q_start_month, 1) - win.grace
        return date(cutoff.year, cutoff.month, 1)
    if g in ("week", "weekly"):
        week_start = today - timedelta(days=today.weekday())
        return week_start - win.grace
    return today - win.grace
