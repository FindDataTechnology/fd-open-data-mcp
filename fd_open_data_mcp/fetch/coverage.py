"""Price-series coverage: decide whether a window is fully covered, and if not,
which intervals are missing.

The judgment is made from **the data itself**: the set of dates on which a
series already has points *is* that series' trading calendar. Prices only exist
on trading days, so counting against an external calendar (or against an assumed
"250 days a year") misjudges every halt, holiday and pre-listing period — and
there is no trading calendar in this service to consult anyway (design D1).

Three facts come out of one pass:

- ``span`` — the series' own covered range (first/last date ever stored);
- ``holes`` — intervals *inside* the window with no points;
- ``gaps`` — the intervals actually missing: the leading/trailing stretches
  outside the covered span, plus any interior hole wide enough to matter.

A cold series (no points at all) reports the whole window missing. That is
deliberately the conservative answer: with no evidence there is no way to tell
"not trading" from "not fetched", so the caller fetches once and the next read
is informed.

Interior holes are filtered by a minimum width because a per-day read history
can leave isolated single-day holes that a range fetch cannot fill anyway; the
threshold is a configuration value, not a constant, so it can be tuned without
changing the shape of the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from fd_open_data_mcp.semantic.dates import observation_period

#: Default minimum width (in days, inclusive) of an interior hole before it is
#: worth fetching. One missing day inside a dense series is usually a real
#: non-trading day or an unfillable per-day artifact; a month is a real hole.
DEFAULT_MIN_HOLE_DAYS = 5

#: Fraction of the series' own dates inside the window that must be present
#: before an interior hole is treated as noise rather than a gap.
DEFAULT_MIN_COVERAGE = 0.98


@dataclass(frozen=True)
class Coverage:
    """What is known about one (series, window)."""

    #: First/last date the series has *any* point on, or None when it has none.
    span_start: str | None
    span_end: str | None
    #: Dates present inside the window.
    present: tuple[str, ...]
    #: Intervals missing from the window, as inclusive ``(start, end)`` pairs.
    gaps: tuple[tuple[str, str], ...]
    #: Subset of ``gaps`` already probed upstream and found to hold nothing.
    #: Reported, but never worth fetching again — these are non-trading stretches
    #: (market holidays), not holes in our copy of the data.
    known_empty: tuple[tuple[str, str], ...] = ()

    @property
    def complete(self) -> bool:
        return not self.gaps

    @property
    def fetchable(self) -> tuple[tuple[str, str], ...]:
        """Gaps worth fetching: present minus already-probed-empty."""
        known = set(self.known_empty)
        return tuple(g for g in self.gaps if g not in known)

    @property
    def cold(self) -> bool:
        """No points anywhere — nothing is known about this series."""
        return self.span_start is None


def _merge(dates: Iterable[str]) -> list[tuple[str, str]]:
    """Collapse sorted dates into inclusive intervals (single days included)."""
    out: list[tuple[str, str]] = []
    for d in sorted(dates):
        if out and _next_day(out[-1][1]) == d:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


def _next_day(day: str) -> str:
    from datetime import date, timedelta

    y, m, d = (int(p) for p in day.split("-"))
    return (date(y, m, d) + timedelta(days=1)).isoformat()


def _prev_day(day: str) -> str:
    from datetime import date, timedelta

    y, m, d = (int(p) for p in day.split("-"))
    return (date(y, m, d) - timedelta(days=1)).isoformat()


def _merge_intervals(intervals: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Sort and coalesce inclusive intervals (adjacent ones touch)."""
    out: list[tuple[str, str]] = []
    for lo, hi in sorted(intervals):
        if out and _next_day(out[-1][1]) >= lo:
            if hi > out[-1][1]:
                out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


def _width(start: str, end: str) -> int:
    from datetime import date

    a = date(*(int(p) for p in start.split("-")))
    b = date(*(int(p) for p in end.split("-")))
    return (b - a).days + 1


def coverage(
    all_dates: Sequence[str],
    start: str,
    end: str,
    *,
    min_hole_days: int = DEFAULT_MIN_HOLE_DAYS,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    known_empty: Sequence[tuple[str, str]] = (),
) -> Coverage:
    """Assess coverage of ``[start, end]`` for a series observed on ``all_dates``.

    ``all_dates`` is every date the series has a point on (any source, any range)
    — not just the dates inside the window. The window's own density is measured
    against the series' dates inside the window, which is why a long series
    yields a stricter judgment than a short one.

    ``known_empty`` carries intervals already probed and found to hold no data
    upstream. Without it, a market holiday looks exactly like a hole: the fill
    asks, gets nothing, and asks again on every subsequent read — six wasted
    upstream calls per read on a two-year A-share window (measured). Recording
    "asked and empty" turns that into one probe, which is also the honest
    description of the situation: the series does not trade there.
    """
    if start > end:
        raise ValueError(f"start ({start}) is after end ({end})")

    def _span_of(day: str) -> tuple[str, str] | None:
        period = observation_period(day)
        return None if period is None else (period[0].isoformat(), period[1].isoformat())

    # 窗口边界也可能是粗粒度的（`start="2019"` 表示整年），先落成具体日子。
    start_span, end_span = _span_of(start), _span_of(end)
    win_lo = start_span[0] if start_span else start
    win_hi = end_span[1] if end_span else end
    whole = sorted(set(all_dates))
    if not whole and not known_empty:
        # Cold: nothing is known. The honest answer is "everything is missing".
        return Coverage(None, None, (), ((win_lo, win_hi),))

    # 窗口内的判定按**日历区间是否相交**，不按字符串大小：
    # `"2019"` 与窗口 `("2019-06-01","2019-08-31")` 字典序上不包含，日历上却相交。
    def _in_window(day: str) -> bool:
        span = _span_of(day)
        return bool(span) and span[0] <= win_hi and span[1] >= win_lo

    dates = sorted({d for d in all_dates if _in_window(d)})

    # 宽度按**这条观测代表的日历区间**算，不按字符串长度：
    # 一条 `2020` 回答的是 2020 全年，一条 `2023-05` 是那个月，一条 `2023-W20`
    # 是那一周。此前这里把日期串直接当"某一天"喂给 _width/_prev_day，
    # 于是按年的序列（本项目多数宏观数据就是按年）第一个日期就抛
    # `ValueError: not enough values to unpack`，异常被上层吞成
    # "coverage unknown"，每一读都降级却没人看得见（11-11 实测 test_read_series 两条红）。
    whole_spans = [sp for sp in (_span_of(d) for d in whole) if sp]
    if not whole_spans:
        # 所有日期都摆不到日历上（LATEST 哨兵、垃圾值）：位置未知，按空算。
        return Coverage(None, None, (), ((start, end),))
    span_start = min(sp[0] for sp in whole_spans)
    span_end = max(sp[1] for sp in whole_spans)

    gaps: list[tuple[str, str]] = []
    for lo, hi in known_empty:
        clipped = (max(lo, win_lo), min(hi, win_hi))
        if clipped[0] <= clipped[1]:
            gaps.append(clipped)
    known = gaps[:]

    # Outer stretches: 第一/最后一个观测**代表**的区间之外，才是真的没覆盖。
    if span_start > win_lo:
        lead = (win_lo, _prev_day(span_start))
        if _width(*lead) >= min_hole_days:
            gaps.append(lead)
    if span_end < win_hi:
        trail = (max(win_lo, _next_day(span_end)), win_hi)
        if trail[0] <= trail[1] and _width(*trail) >= min_hole_days:
            gaps.append(trail)

    # Interior: 先按"代表区间"合并出实际覆盖到的日子，再在它们之间找洞。
    present_spans = [sp for sp in (_span_of(d) for d in dates) if sp]
    present = _merge_intervals(present_spans)
    inner_lo = max(win_lo, span_start)
    inner_hi = min(win_hi, span_end)
    if inner_lo <= inner_hi:
        clipped_present = [(max(a, inner_lo), min(b, inner_hi)) for a, b in present]
        clipped_present = [(a, b) for a, b in clipped_present if a <= b]
        covered_days = sum(_width(a, b) for a, b in clipped_present)
        span_days = _width(inner_lo, inner_hi)
        if span_days > 0 and covered_days / span_days < min_coverage:
            cursor = inner_lo
            for hole_start, hole_end in clipped_present:
                if cursor < hole_start:
                    if _width(cursor, _prev_day(hole_start)) >= min_hole_days:
                        gaps.append((cursor, _prev_day(hole_start)))
                cursor = max(cursor, _next_day(hole_end))
            if cursor <= inner_hi and _width(cursor, inner_hi) >= min_hole_days:
                gaps.append((cursor, inner_hi))

    merged = _merge_intervals(gaps)
    # Intervals already known to be empty are reported but never re-fetched, so
    # they are separated back out for the caller.
    return Coverage(
        span_start=span_start,
        span_end=span_end,
        present=tuple(dates),
        gaps=tuple(merged),
        known_empty=tuple(_merge_intervals(known)),
    )
