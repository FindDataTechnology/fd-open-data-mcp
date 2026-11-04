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
"""
from __future__ import annotations

import re

_BARE_YEAR = re.compile(r"^\d{4}$")
_BARE_MONTH = re.compile(r"^\d{4}-\d{2}$")
_ISO_WEEK = re.compile(r"^\d{4}-W\d{2}$")


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
