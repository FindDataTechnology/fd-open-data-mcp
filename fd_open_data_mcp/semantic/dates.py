"""Observation date governance: the derived partition bucket (``obs_month``).

Census 2026-11-04 (see reports/2026-11-04-capacity-census.md): the ``date``
column carries six shapes in production, not just canonical YYYY-MM-DD —
bare years from a cnreport writer defect, hour-truncated timestamps, bare
months, ISO weeks, and a single ``LATEST`` sentinel. The partition migration
(monthly RANGE partitions, add-data-stream-engine task 2.6) needs a reliable
month bucket WITHOUT rewriting any stored value (rewrites risk uq_sem_obs
collisions and break readers that match the stored shape — the read path
deliberately tolerates bare forms, see ``fetch.cache.period_prefixes``).

``obs_month_of`` is that mapping: text ``'YYYY-MM'`` for every mappable
shape, ``None`` for unmappable (sentinels) — those land in the default
partition. The same CASE logic runs in alembic 0013 (SQL) and at the write
sites (this helper); keep the two in sync.

The *calendar* meaning of those same shapes lives here too
(``observation_period`` / ``window_span`` / ``sql_bounds``). The column is
text, so any range predicate on it is a lexicographic comparison, and the six
shapes order wrongly under one: ``"2023" < "2023-01-01"`` (a bare year falls
before any day inside it) and ``"2026-W40" > "2026-12-31"`` (``'W' > '1'``).
``sql_bounds`` widens a requested window to whole years for the SQL predicate
and ``observation_period`` decides each returned row on its parsed span, which
is how the range reader (``fetch.cache.read_cache_range``) stops dropping
whole years silently.
"""
from __future__ import annotations

import datetime as _dt
import re

_BARE_YEAR = re.compile(r"^\d{4}$")
_BARE_MONTH = re.compile(r"^\d{4}-\d{2}$")
_ISO_WEEK = re.compile(r"^\d{4}-W\d{2}$")

#: SQL ``LIKE`` equivalents of the shapes above — bare year, bare month,
#: day/timestamp, ISO week. Used to *find* (not interpret) rows whose date fits
#: no tolerated shape; ``fetch.cache._unmappable_dates`` builds its exclusion
#: predicate from this tuple so it cannot drift from the regexes.
SHAPE_LIKE_PATTERNS = ("____", "____-__", "____-__-__%", "____-W__")


def obs_month_of(date: str | None) -> str | None:
    """Month bucket 'YYYY-MM' for an observation date of any tolerated shape.

    Canonical/YYYY-MM-DD and hour-truncated ('2026-10-06T02…') → first 7 chars.
    Bare year → that December (annual figures anchor the period end; the
    bucket is only a partition key, value semantics are untouched).
    Bare month → itself. ISO week → the month containing that week's MONDAY
    (ISO calendar). Anything else (sentinels, garbage) → None: default
    partition territory, observable and re-mappable without a rewrite.
    """
    if not date:
        return None
    if _ISO_WEEK.match(date):
        try:
            from datetime import date as _d

            year, week = int(date[:4]), int(date[6:8])
            return _d.fromisocalendar(year, week, 1).strftime("%Y-%m")
        except ValueError:
            return None
    if _BARE_YEAR.match(date):
        return f"{date}-12"
    if _BARE_MONTH.match(date) or (len(date) >= 7 and date[4] == "-" and
                                   date[:4].isdigit() and date[5:7].isdigit()):
        return date[:7]
    return None


def observation_period(date: str | None) -> tuple[_dt.date, _dt.date] | None:
    """The calendar span a stored observation date speaks for, or ``None``.

    ``(first_day, last_day)`` for the same six shapes ``obs_month_of`` maps:

    * ``YYYY-MM-DD`` and hour-truncated timestamps (``2026-10-06T02…``) → that
      one day;
    * bare ``YYYY`` → the whole year (that row answers any date inside it, the
      same convention ``fetch.cache.period_prefixes`` applies to point reads);
    * bare ``YYYY-MM`` → that month;
    * ``YYYY-Www`` → that ISO week's Monday..Sunday;
    * anything else (the ``LATEST`` first-crawl sentinel, garbage) → ``None``:
      no calendar position, so no window can contain it.
    """
    if not date:
        return None
    d = date.strip()
    try:
        if _ISO_WEEK.match(d):
            monday = _dt.date.fromisocalendar(int(d[:4]), int(d[6:8]), 1)
            return monday, monday + _dt.timedelta(days=6)
        if _BARE_YEAR.match(d):
            year = int(d)
            return _dt.date(year, 1, 1), _dt.date(year, 12, 31)
        if _BARE_MONTH.match(d):
            year, month = int(d[:4]), int(d[5:7])
            first = _dt.date(year, month, 1)
            following = _dt.date(year + (month == 12), month % 12 + 1, 1)
            return first, following - _dt.timedelta(days=1)
        day = _dt.date.fromisoformat(d[:10])  # canonical day, or a timestamp's day
    except ValueError:
        return None
    return day, day


def window_span(start: str | None, end: str | None) -> tuple[_dt.date, _dt.date] | None:
    """``(first_day, last_day)`` requested by ``start``/``end``, inclusive.

    A bound is read with the same tolerance as a stored date: a bare ``2023``
    as ``start`` means the whole of 2023, which is also how a bare-year row was
    written. ``None`` when either bound is not a date — the caller then keeps
    its previous lexicographic meaning rather than guessing.
    """
    first, last = observation_period(start), observation_period(end)
    if first is None or last is None:
        return None
    return first[0], last[1]


def sql_bounds(start: str, end: str) -> tuple[str, str]:
    """String bounds that cannot exclude a shape inside ``[start, end]``.

    The predicate stays in SQL (so the index is still used), but it is widened
    to whole years: ``"2023"`` for the lower bound (a bare-year row sorts below
    every ``2023-…`` string) and ``"2023-W99"`` for the upper one (``'W'``
    sorts after any digit, so this string is above every ``YYYY-…`` shape,
    including the ISO week and timestamp forms). The result is a superset of
    the window; the exact per-row decision is made on the parsed span
    (:func:`observation_period`), never on the string.

    Falls back to the literal bounds when either side is unparseable — the
    caller then keeps the old behaviour instead of an invented window.
    """
    span = window_span(start, end)
    if span is None:
        return start, end
    return f"{span[0].year:04d}", f"{span[1].year:04d}-W99"
