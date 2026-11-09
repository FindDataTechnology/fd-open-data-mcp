"""Concept-fetch dispatch: read-through cache + ranked failover.

read(concept, entity, dates):
  1. check_applicability (entity type matches concept) - raises on mismatch.
  2. For each date: read cache -> return if fresh; else dispatch.
  3. dispatch: rank sources -> for each (source, binding) in rank order:
       resolve the entity id for that source (skip if missing - graceful
       degradation); run the upstream callable; on success write cache +
       record outcome + sample-confirm the binding; on failure record outcome
       and fail over to the next source/binding.
  4. If nothing succeeded: return an error row for that date.

v1 limitations (see design.md open questions): ``_build_params`` and
``_extract_value`` are best-effort and do not yet handle per-function
date-format / payload-shape quirks. A production runner would refine these
per provider (reference: fd-akshare / fd-yfinance runner patterns).

real_source failover:
  When a function declares ``real_sources`` (e.g., eastmoney, tencent), the
  dispatcher tries each real_source in priority order. If the primary real_source
  is banned (all proxies OPEN), it fails over to the next priority real_source.
  This is logged at INFO level for visibility.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.entities.resolver import check_applicability, resolve_identifier
from fd_open_data_mcp.fetch.cache import (
    is_stale,
    read_cache,
    read_cache_all,
    read_cache_range,
    write_cache,
    write_cache_range,
)
from fd_open_data_mcp.fetch.finalization import finalized_through, resolve_window
from fd_open_data_mcp.fetch.instrumentation import SourceUnavailable, instrumented_fetch
from fd_open_data_mcp.fetch.runner import FetchError, returned_columns
from fd_open_data_mcp.models import Concept, ConceptBinding, Function
from fd_open_data_mcp.ranking.scorer import rank_sources_for_concept, record_fetch_outcome
from fd_open_data_mcp.semantic.bindings import dispatch_candidates, promote_on_sample


def _write_granularity(frequency: str | None) -> str:
    """概念频率 → 写入用的 granularity 标签（day|month|year）。

    必须与 `crawl/planner._granularity_for` 同义：读路径与爬虫路径若用不同标签
    标同一个数据点，去重键 `(date, granularity)` 会把它们当两行——月度概念在
    爬虫行 TTL 过期后被 read 一次，就会并排出 day 行，`read_series` 于是同一
    日期返回两次，求和/连乘双计。

    刻意不 import planner：planner 属 crawl 层，fetch 层反向依赖会把整个抓取
    调度拉进读路径。两处实现同义由 `tests/test_write_granularity.py` 对着断言。
    """
    if frequency == "yearly":
        return "year"
    if frequency == "monthly":
        return "month"
    return "day"

THRESHOLD = 0.6
logger = logging.getLogger(__name__)


def _ms(t0: datetime) -> int:
    return int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)


def _bindings_for_source(session: Session, concept_id: int, source_name: str) -> list[tuple[ConceptBinding, Function]]:
    cands = dispatch_candidates(session, concept_id, THRESHOLD)
    out = []
    for b in cands:
        fn = session.get(Function, b.column.function_id)
        if fn is None:
            continue
        # column-level datasource overrides the function's source (composite columns)
        eff_source = b.column.datasource or fn.source.name
        if eff_source == source_name and fn.verified:
            out.append((b, fn))
    return out


def _build_params(fn: Function, identifier: str, date: str, binding=None) -> dict:
    """Param mapping: delegate to the adapter registry if one is registered for
    ``(source, command)`` (design.md D4); otherwise fall back to the legacy
    best-effort name-guessing. The adapter is the authority where present."""
    from fd_open_data_mcp.adapters import adapter_for

    adapter = adapter_for(fn.source.name, fn.command)
    if adapter is not None:
        return adapter.build_params(fn, identifier, date, binding)

    # legacy best-effort: entity id -> symbol/economy; date -> start/end;
    # indicator code -> binding's column name (for wbgapi).
    params: dict = {}
    for p in (fn.parameters or []):
        name = p.get("name")
        if name is None:
            continue
        lname = name.lower()
        if lname in ("symbol", "code", "ticker", "stock", "economy", "country"):
            params[name] = identifier
        elif lname in ("date", "start_date", "start", "end_date", "end"):
            params[name] = date
        elif lname == "indicator" and binding is not None:
            params[name] = binding.column.name
        # optional params with no mapping are left unset
    return params


def _declared_unit(result) -> Optional[str]:
    """The unit the *source* declared for this result, if it declared one.

    Some adapters emit a ``unit`` column next to the value (``nbs_gdp`` writes
    ``十亿元`` / ``亿元`` / ``指数``). The write path used to ignore it and stamp
    the concept's unit on every row, so a frame whose native unit differs was
    silently relabelled — the number *is* in 亿元, calling it the concept's unit
    makes every aggregated read wrong by the conversion factor, with no field
    saying so (audit 04 §6).

    Returns ``None`` when the source said nothing (most adapters) or when the
    frame carries conflicting units — in both cases the caller falls back to the
    concept's unit, which is the previous behaviour. Guessing here would be
    worse than the old silence.
    """
    try:
        import pandas as pd
    except ImportError:
        pd = None
    if pd is not None and isinstance(result, pd.DataFrame) and "unit" in result.columns:
        seen = {str(v).strip() for v in result["unit"].dropna().unique() if str(v).strip()}
        return seen.pop() if len(seen) == 1 else None
    if isinstance(result, dict):
        raw = result.get("unit")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _missing(value) -> bool:
    """True when ``value`` means "no data" rather than "data worth keeping".

    Covers ``None``, float ``NaN`` (including numpy's), ``pandas.NaT`` and
    ``pandas.NA``. The ``value != value`` test is the portable one: NaN is the
    only thing that is not equal to itself, and both NaT and NA also fail
    equality. ``None`` is checked first because ``None == None`` is True.

    Why this exists: the legacy DataFrame lookup returns the raw cell, and the
    caller only rejected ``if value is None``. A NaN sailed through, was written
    as ``str(value)`` → the literal text ``"nan"``, and rows whose period has
    elapsed are period-final and never re-fetched — so one bad write made
    ``"nan"`` a permanent "observation" that also recorded a *successful* fetch.
    """
    if value is None:
        return True
    # pandas 的 NA/NaT 让 `value != value` 返回 pd.NA 而不是 bool，`bool(pd.NA)`
    # 会抛 TypeError——所以走 pandas 自己的判据（pandas 已在函数入口按需 import）。
    try:
        import pandas as pd

        if pd.isna(value) is True:
            return True
    except (ImportError, TypeError, ValueError):
        pass
    try:
        return bool(value != value)
    except (TypeError, ValueError):
        return False


def _extract_value(result, column_name: str, date: str, source: Optional[str] = None, command: Optional[str] = None,
                   identifier: Optional[str] = None):
    """Value extraction: delegate to the adapter registry if one is registered for
    ``(source, command)`` (design.md D4); otherwise fall back to the legacy
    best-effort DataFrame lookup. Returns ``None`` when no value is found (callers
    treat ``None`` as a fetch failure and fail over). ``identifier`` is forwarded
    to adapters that row-pick rank frames (no date axis)."""
    from fd_open_data_mcp.adapters import adapter_for

    if source and command:
        adapter = adapter_for(source, command)
        if adapter is not None:
            return adapter.extract_value(result, column_name, date, identifier=identifier)

    try:
        import pandas as pd
    except ImportError:
        return None
    if not isinstance(result, pd.DataFrame) or column_name not in result.columns:
        return None
    df = result
    # find a date-like column to index on
    date_col = None
    for c in df.columns:
        if str(c).lower() in ("date", "日期", "datetime", "时间"):
            date_col = c
            break
    if date_col is not None:
        df = df.set_index(date_col)
    idx_str = [str(i) for i in df.index]
    if date in idx_str:
        cell = df.iloc[idx_str.index(date)][column_name]
        return None if _missing(cell) else cell
    # tolerate YYYY-MM-DD vs YYYYMMDD
    compact = date.replace("-", "")
    if compact in idx_str:
        cell = df.iloc[idx_str.index(compact)][column_name]
        return None if _missing(cell) else cell
    return None


def _get_real_sources(fn: Function) -> list[dict]:
    """Extract real_sources from Function, sorted by priority (0 = primary).

    Returns empty list if no real_sources declared (backward compatibility).
    """
    if not fn.real_sources:
        return []
    # real_sources is stored as JSONB: [{"name": "eastmoney", "priority": 0}, ...]
    return sorted(fn.real_sources, key=lambda rs: rs.get("priority", 999))


def dispatch_one(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    date: str, requested_date: Optional[str] = None,
    source: Optional[str] = None,
) -> Optional[dict]:
    """Fetch one (concept, entity, date) with cache + ranked failover.

    Supports real_source-based failover: when a function declares multiple
    real_sources (e.g., eastmoney, tencent), tries each in priority order.
    ``source`` pins the read AND the dispatch to one source: the cache is read
    for that source only, and ranked candidates outside it are skipped (its
    fresh row is served; its stale/missing row is refreshed from it).
    """
    concept = session.get(Concept, concept_id)
    if concept is None:
        return None

    obs = read_cache(session, concept_id, entity_type, entity_id, date, source=source,
                     frequency=concept.frequency)
    if obs is not None and not is_stale(obs, concept.frequency):
        # `value` is a VARCHAR column written as str(value), so a cache hit would
        # otherwise hand back "1650.0" where a fresh fetch hands back 1650.0 —
        # in the SAME read() call, since one date can be cached and the next not.
        # Clients then hit `TypeError: can't concat float to str` on sum().
        # The range path already coerces (`_coerce_value`); this is the missed one.
        return {"date": date, "value": _coerce_value(obs.value), "unit": obs.unit,
                "source_used": obs.source_used, "from_cache": True}

    # Why a read can come back empty, kept distinct so callers can act:
    # "no source succeeded" (sources were tried and failed) is a different fact
    # from "no eligible source" (nothing to try — the concept has no confirmed
    # binding) and from "no identifier" (the entity is unmapped for every
    # candidate source). The generic message used to cover all three, which read
    # as a transient failure for what is actually a coverage gap.
    candidates_seen = 0
    identifiers_resolved = 0
    attempts = 0

    for cand in rank_sources_for_concept(session, concept_id, requested_date):
        cand_source = cand["source"]
        if source is not None and cand_source != source:
            continue  # source-pinned read: never fetch from another source
        candidates_seen += 1
        identifier = resolve_identifier(session, entity_type, entity_id, cand_source)
        if identifier is None:
            continue  # graceful degradation: no per-source id for this entity
        identifiers_resolved += 1
        for binding, fn in _bindings_for_source(session, concept_id, cand_source):
            attempts += 1
            params = _build_params(fn, identifier, date, binding)

            # Get real_sources for this function (if declared)
            real_sources = _get_real_sources(fn)

            # Try each real_source in priority order (or just the library source if none declared)
            sources_to_try = real_sources if real_sources else [{"name": cand_source, "priority": 0}]

            for real_source_spec in sources_to_try:
                real_source = real_source_spec.get("name")
                t0 = datetime.now(timezone.utc)
                try:
                    # Routed through the shared instrumentation: per-fetch proxy
                    # selection + ban classification + circuit recording. Raises
                    # SourceUnavailable when every proxy for this source is OPEN ->
                    # fail over to the next real_source or source.
                    result = instrumented_fetch(
                        cand_source, fn.command, params,
                        real_source=real_source,  # Pass real_source for circuit tracking
                        session=session, function_id=fn.id,
                        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
                    )
                except SourceUnavailable:
                    # All proxies for this real_source are down
                    if len(sources_to_try) > 1:
                        # Log failover event
                        logger.info(f"real_source failover: {real_source} -> trying next priority")
                    record_fetch_outcome(session, cand_source, concept_id, "error", _ms(t0))
                    continue  # Try next real_source
                except FetchError:
                    record_fetch_outcome(session, cand_source, concept_id, "error", _ms(t0))
                    continue
                except Exception:  # noqa: BLE001 - any upstream failure -> failover
                    record_fetch_outcome(session, cand_source, concept_id, "error", _ms(t0))
                    continue

                # Success!
                latency = _ms(t0)
                value = _extract_value(result, binding.column.name, date, cand_source, fn.command,
                                       identifier=identifier)
                if value is None:
                    record_fetch_outcome(session, cand_source, concept_id, "error", latency)
                    continue
                record_fetch_outcome(session, cand_source, concept_id, "ok", latency)
                promote_on_sample(session, fn.id, returned_columns(result))
                declared = _declared_unit(result)
                if declared is not None and concept.unit and declared != concept.unit:
                    logger.warning(
                        "unit mismatch for concept %s from %s: source says %r, "
                        "concept says %r — storing the source's unit",
                        concept_id, cand_source, declared, concept.unit,
                    )
                write_cache(session, concept_id, entity_type, entity_id, date,
                            str(value), declared or concept.unit, cand_source,
                            granularity=_write_granularity(concept.frequency))
                return {"date": date, "value": value, "unit": concept.unit,
                        "source_used": cand_source, "real_source_used": real_source, "from_cache": False}

    if candidates_seen == 0:
        return {"date": date, "value": None, "error": "no eligible source",
                "detail": "no confirmed binding for this concept (bind it, or run propose_bindings/confirm_binding)",
                "series_state": _series_state(session, concept_id, entity_id)}
    if identifiers_resolved == 0:
        return {"date": date, "value": None, "error": "no source could resolve an identifier for this entity",
                "detail": "entity has no per-source identifier for any candidate source (see entity_source_identifiers)",
                "series_state": _series_state(session, concept_id, entity_id)}
    if attempts == 0:
        return {"date": date, "value": None, "error": "no dispatchable binding",
                "detail": "candidate sources resolved identifiers but hold no verified binding for this concept",
                "series_state": _series_state(session, concept_id, entity_id)}
    return None


def _series_state(session: Session, concept_id: int, entity_id: int) -> str:
    """Error-row 归因：never_held=确认没有（该 concept+entity 从未有任何源持有过值）；
    gap_in_series=未取得（源在产、其他日期有值，本日期是缺口）。

    跨境查询面四类归因的查询端（add-crossborder-vertical-line 2.6）：采集端
    归因（配额/上游/防护/待人工）由 spider 层 `_error_category` 携带，查询端
    只区分「未取得」与「确认没有」——两者在报数面上必须可区分。
    """
    from sqlalchemy import text

    row = session.execute(
        text("select 1 from semantic_observations "
             "where concept_id = :c and entity_id = :e limit 1"),
        {"c": concept_id, "e": entity_id},
    ).first()
    return "gap_in_series" if row else "never_held"


def read(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    dates: list[str], requested_date: Optional[str] = None,
    source: Optional[str] = None, all_sources: bool = False,
) -> list[dict]:
    """Read a concept for an entity over a list of dates (read-through + dispatch).

    ``source`` pins both cache reads and dispatch to that source. ``all_sources``
    returns every held row per date (best-ranked first, one per source) and
    never dispatches — comparison of what is held; a missing source is a
    coverage gap to fill with a crawl policy, not a surprise fetch.
    """
    check_applicability(session, concept_id, entity_type)
    # Defensive cap: each date may trigger a ranked network fetch; an unbounded
    # date list (e.g. 3 years of daily = ~730) can hold the MCP connection open
    # long enough to drop. Bulk history belongs in the migrate/astock_daily path,
    # not read(). Cap and warn; callers page for larger ranges.
    MAX_DATES = 366
    if len(dates) > MAX_DATES:
        # Keep the TAIL, not the head. The old `dates[:MAX_DATES]` kept the
        # oldest 366 of the list, so a caller asking for five years of daily
        # prices got rows ending ~four years in the past and no hint that the
        # recent ones were dropped — a "latest close" query silently answered
        # with a 2021 value. read_series already resolves this the same way
        # (`rows[-MAX_SERIES_ROWS:]` plus a note); read() still has no place to
        # put a note because it returns a bare list, so the tail choice plus
        # this warning is the honest minimum.
        logger.warning(
            "read() requested %d dates for concept %s entity %s; keeping the most "
            "recent %d. Use read_series (which also reports the truncation) for "
            "long windows.",
            len(dates), concept_id, entity_id, MAX_DATES,
        )
        dates = dates[-MAX_DATES:]
    if all_sources:
        results = []
        for d in dates:
            rows = read_cache_all(session, concept_id, entity_type, entity_id, d)
            for r in rows:
                results.append({"date": d, "value": _coerce_value(r.value),
                                "unit": r.unit, "source_used": r.source_used,
                                "from_cache": True})
            if not rows:
                results.append({"date": d, "value": None,
                                "error": "no source holds a value",
                                "series_state": _series_state(session, concept_id, entity_id)})
        return results
    results = []
    for d in dates:
        r = dispatch_one(session, concept_id, entity_type, entity_id, d, requested_date,
                         source=source)
        if r is None:
            results.append({"date": d, "value": None, "error": "no source succeeded",
                            "series_state": _series_state(session, concept_id, entity_id)})
        else:
            results.append(r)
    return results


# --- read_range: bulk series fetch -------------------------------------------


def _normalize_date(value: object) -> str:
    """Coerce a date cell to 'YYYY-MM-DD' for series extraction.

    akshare/yfinance return python ``date``/``datetime`` or string forms
    ('YYYY-MM-DD', 'YYYYMMDD'); we normalize so the range filter and cache
    keys are stable.
    """
    from datetime import date as _date, datetime as _dt

    if value is None:
        return ""
    if isinstance(value, _dt):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, _date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _build_range_params(fn: Function, identifier: str, start: str, end: str, binding=None) -> dict:
    """Param mapping for a range fetch.

    Adapter-first: if the adapter defines ``build_range_params`` (the akshare
    daily adapters do), delegate. Otherwise fall back to the single-date
    ``build_params`` with ``start`` and override any end-named kwarg with ``end``
    (covers yfinance's ``Ticker.history(start, end)`` and similar). The legacy
    no-adapter path maps start/end params by name, mirroring ``_build_params``.
    """
    from fd_open_data_mcp.adapters import adapter_for

    adapter = adapter_for(fn.source.name, fn.command)
    if adapter is not None and hasattr(adapter, "build_range_params"):
        return adapter.build_range_params(fn, identifier, start, end, binding)
    if adapter is not None:
        params = adapter.build_params(fn, identifier, start, binding)
        for key in list(params):
            if key.lower() in ("end", "end_date"):
                params[key] = end
        return params
    # legacy best-effort
    params: dict = {}
    for p in (fn.parameters or []):
        name = p.get("name")
        if name is None:
            continue
        lname = name.lower()
        if lname in ("start", "start_date"):
            params[name] = start
        elif lname in ("end", "end_date"):
            params[name] = end
        elif lname == "date":
            params[name] = start
        elif lname in ("symbol", "code", "ticker", "stock", "economy", "country"):
            params[name] = identifier
        elif lname == "indicator" and binding is not None:
            params[name] = binding.column.name
    return params


def _extract_series(
    result, column_name: str, start: str, end: str,
    source: Optional[str] = None, command: Optional[str] = None,
) -> dict:
    """Pull every (date, column_name) cell with start <= date <= end.

    Adapter-first: if the adapter defines ``extract_series`` (the akshare base
    does), delegate. Otherwise fall back to legacy DataFrame extraction — find a
    date column (or use the index), normalize each row's date, filter to the
    range, skip NaN. Returns ``{'YYYY-MM-DD': value}``.
    """
    from fd_open_data_mcp.adapters import adapter_for

    if source and command:
        adapter = adapter_for(source, command)
        if adapter is not None and hasattr(adapter, "extract_series"):
            return adapter.extract_series(result, column_name, start, end)
    try:
        import pandas as pd
    except ImportError:
        return {}
    if not isinstance(result, pd.DataFrame) or result.empty:
        return {}
    if column_name not in result.columns:
        return {}
    date_col = None
    for c in result.columns:
        if str(c).lower() in ("date", "日期", "datetime", "时间"):
            date_col = c
            break
    if date_col is not None:
        dates = [_normalize_date(v) for v in result[date_col].tolist()]
        values = result[column_name].tolist()
    else:
        dates = [_normalize_date(v) for v in result.index.tolist()]
        values = result[column_name].tolist()
    out: dict = {}
    for d, val in zip(dates, values):
        if d and start <= d <= end and not pd.isna(val):
            out[d] = val
    return out


def read_range_with_coverage(
    session: Session, concept_ids: list[int], entity_type: str, entity_id: int,
    start: str, end: str,
    *, min_hole_days: float | None = None, min_coverage: float | None = None,
    revision_reason: str | None = None,
) -> dict[int, dict]:
    """读区间并**声明覆盖与降级**（change: define-ingest-revision-and-coverage）。

    为什么需要这个入口
    -----------------
    ``read_range`` 的返回体只有值。它的 docstring 自己写着覆盖度"只按陈旧度判"：
    一个窗口里既有新鲜行又缺一段，读起来和"完整"完全一样。消费方据此算出来的
    收益率、同比、缺口统计都可能建立在半份数据上，而没有任何信号。

    本函数不改变 ``read_range`` 的行为（老调用方零影响），只多给两件事：

      ``coverage``  ``{requested, covered, missing}``——窗口内实际覆盖的区间与
                    缺失区间。判定复用 ``fetch.coverage.coverage()``，日期基准是
                    序列自身的日历（与补数同一套），不引外部交易日历。
      ``degraded``  ``{degraded, reason, sources_failed}``——回源全失败并退回缓存，
                    或干脆没有数据时，这里必须为真。**"取不到"不能长得像"没有"**。

    ``covered`` 为空的含义要分清：窗口内确实没有数据（冷序列）与"有数据但都在窗口
    之外"都表现为空，前者 ``missing`` 覆盖整个窗口，后者由 ``coverage`` 的 span 说明。
    """
    from fd_open_data_mcp.fetch.cache import _read_base_range
    from fd_open_data_mcp.fetch.coverage import _merge_intervals, coverage

    if not concept_ids or start > end:
        return {}

    kwargs: dict = {}
    if min_hole_days is not None:
        kwargs["min_hole_days"] = min_hole_days
    if min_coverage is not None:
        kwargs["min_coverage"] = min_coverage

    out: dict[int, dict] = {}
    for concept_id in concept_ids:
        ex = _read_range_one_ex(
            session, concept_id, entity_type, entity_id, start, end,
            revision_reason=revision_reason,
        )

        all_dates = sorted({
            r.date for r in _read_base_range(
                session, concept_id, entity_type, entity_id, "0000-01-01", "9999-12-31",
            )
        })
        report = coverage(all_dates, start, end, **kwargs)
        in_window = [d for d in all_dates if start <= d <= end]
        covered = [list(iv) for iv in _merge_intervals((d, d) for d in in_window)]

        served = ex["served_from"]
        failed = ex["sources_failed"]
        degraded = served == "cache_degraded"
        if served == "none" and failed:
            # 尝试过来源、全都失败、缓存也没有 —— 这不是"没有数据"，是"没读到"。
            degraded = True
        reason = None
        if served == "cache_degraded":
            reason = ("upstream unavailable; served cached rows "
                      f"({len(ex['values'])} point(s), {ex['stale_in_window']} stale)")
        elif degraded:
            reason = f"all sources failed ({', '.join(sorted(set(failed)))})"

        out[concept_id] = {
            "values": ex["values"],
            "coverage": {
                "requested": [start, end],
                "covered": covered,
                "missing": [list(g) for g in report.gaps],
                "known_empty": [list(g) for g in report.known_empty],
                "complete": report.complete,
            },
            "degraded": {
                "degraded": degraded,
                "reason": reason,
                "sources_failed": failed,
            },
            "served_from": served,
        }
    return out


def read_range(
    session: Session, concept_ids: list[int], entity_type: str, entity_id: int,
    start: str, end: str, revision_reason: str | None = None,
) -> dict[int, list[dict]]:
    """Bulk read a list of concepts for an entity over ``[start, end]``.

    For each concept:
      1. Read cached observations in range. Fresh rows (not stale per the
         concept's frequency) go into the result.
      2. If any cached row is stale OR no rows exist at all, do ONE ranked
         range fetch (per ranked source, one upstream call covering the full
         range), bulk-write the extracted series to cache, and return the
         full cached range.

    Coverage caveat: partial-coverage detection is by staleness only. A range
    with some fresh-but-incomplete cached rows (e.g. from old per-date reads)
    returns what's cached without re-fetching. A cold range (no rows) always
    fetches.

    Returns ``{concept_id: [{date, value, unit, source_used}, ...]}`` sorted
    by date; concepts with no data get an empty list.
    """
    if not concept_ids or start > end:
        return {c: [] for c in concept_ids}
    out: dict[int, list[dict]] = {}
    for concept_id in concept_ids:
        out[concept_id] = _read_range_one_ex(
            session, concept_id, entity_type, entity_id, start, end,
            revision_reason=revision_reason,
        )["values"]
    return out


def _coerce_value(value):
    """Cached observations store values as strings; coerce numerics back so the
    range API returns the same types on a cache hit as on a fresh fetch."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _plan_range_read(cached, concept, start: str, end: str) -> dict:
    """把区间读拆成「缓存可服务」与「需要回源」两段。

    纯函数（不碰库、不发请求），因此可以脱离数据库单测——这是"放宽定型窗口不会
    变成上游流量事件"这条要求的守卫点。

    规则：
      * 日期早于 ``finalized_through`` 切点的行已定型，永不回源。
      * 未定型段里按 TTL 判陈旧；只要有一段陈旧（或该段根本没有行）才回源。
      * 回源起点取 ``max(start, cutoff)``，所以已定型的历史段不会被拖进请求。
      * 切点晚于窗口末尾（整窗已成定型）时 ``need_fetch`` 为 False。
    """
    granularity = _write_granularity(getattr(concept, "frequency", None))
    cutoff = finalized_through(
        granularity, resolve_window(granularity, concept)
    ).isoformat()

    finalized = [r for r in cached if r.date < cutoff]
    mutable_tail = [r for r in cached if r.date >= cutoff]
    tail_stale = [r for r in mutable_tail if is_stale(r, getattr(concept, "frequency", None))]

    fetch_start = max(start, cutoff)
    need_fetch = fetch_start <= end and bool(tail_stale or not mutable_tail)

    # 冷区间（整窗没有任何缓存行）是**缺口/冷启动**，不是"已定型历史"——没有可保护的
    # 值，整窗回源才是对的。把它也挡掉会让历史区间永远补不了数。
    if not cached:
        fetch_start = start
        need_fetch = start <= end

    return {
        "cutoff": cutoff,
        "finalized": finalized,
        "mutable_tail": mutable_tail,
        "tail_stale": tail_stale,
        "stale_count": len(tail_stale),
        "fetch_start": fetch_start,
        "need_fetch": need_fetch,
        "cold": not cached,
    }


def _read_range_one_ex(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str, revision_reason: str | None = None,
) -> dict:
    """``_read_range_one`` 加上本次读取的自证信息（change:
    define-ingest-revision-and-coverage）。

    返回值形状：
      ``values``        行列表（与旧 ``_read_range_one`` 完全同形）
      ``served_from``   ``"cache"``（命中且无陈旧）| ``"fetch"``（回源后返回）
                        | ``"cache_degraded"``（回源全失败、退回缓存）| ``"none"``
      ``sources_failed`` 尝试过但失败/无数据的来源（去重后按尝试顺序）
      ``stale_in_window`` 窗口内读到但被判陈旧的缓存行数

    把"回源失败后退回缓存"标成 ``cache_degraded`` 是关键：旧实现里它与"缓存
    本来就新鲜"返回同一个空/旧结果，调用方无法区分"没有数据"和"取不到数据"。
    """
    concept = session.get(Concept, concept_id)
    if concept is None:
        return {"values": [], "served_from": "none", "sources_failed": [],
                "stale_in_window": 0, "cold": True}
    check_applicability(session, concept_id, entity_type)

    cached = read_cache_range(session, concept_id, entity_type, entity_id, start, end)

    # 分区：已定型的行永不回源；只有未定型且过期的尾段才回源。
    plan = _plan_range_read(cached, concept, start, end)
    finalized = plan["finalized"]
    mutable_tail = plan["mutable_tail"]
    stale_count = plan["stale_count"]
    fetch_start = plan["fetch_start"]
    need_fetch = plan["need_fetch"]

    if not need_fetch:
        return {
            "values": [
                {"date": r.date, "value": _coerce_value(r.value), "unit": r.unit,
                 "source_used": r.source_used}
                for r in sorted(cached, key=lambda r: r.date)
            ],
            "served_from": "cache", "sources_failed": [],
            "stale_in_window": 0, "cold": False,
        }

    failed: list[str] = []

    # Fetch path: ranked sources, one upstream range call per (source, binding).
    for cand in rank_sources_for_concept(session, concept_id, None):
        source = cand["source"]
        identifier = resolve_identifier(session, entity_type, entity_id, source)
        if identifier is None:
            continue
        for binding, fn in _bindings_for_source(session, concept_id, source):
            params = _build_range_params(fn, identifier, fetch_start, end, binding)
            real_sources = _get_real_sources(fn)
            sources_to_try = real_sources if real_sources else [{"name": source, "priority": 0}]
            for real_source_spec in sources_to_try:
                real_source = real_source_spec.get("name")
                t0 = datetime.now(timezone.utc)
                try:
                    result = instrumented_fetch(
                        source, fn.command, params,
                        real_source=real_source,
                        session=session, function_id=fn.id,
                        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
                    )
                except SourceUnavailable:
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    failed.append(str(real_source or source))
                    continue
                except FetchError:
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    failed.append(str(real_source or source))
                    continue
                except Exception:  # noqa: BLE001
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    failed.append(str(real_source or source))
                    continue

                series = _extract_series(
                    result, binding.column.name, fetch_start, end, source, fn.command,
                )
                if not series:
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    failed.append(str(real_source or source))
                    continue
                record_fetch_outcome(session, source, concept_id, "ok", _ms(t0))
                promote_on_sample(session, fn.id, returned_columns(result))
                # Same rule as the point write: a source that declares its own
                # unit wins over the concept's label, because the numbers really
                # are in that unit.
                declared = _declared_unit(result)
                if declared is not None and concept.unit and declared != concept.unit:
                    logger.warning(
                        "unit mismatch for concept %s from %s: source says %r, "
                        "concept says %r — storing the source's unit",
                        concept_id, source, declared, concept.unit,
                    )
                write_cache_range(
                    session, concept_id, entity_type, entity_id,
                    series, declared or concept.unit, source,
                    granularity=_write_granularity(concept.frequency),
                    # 声明式重取：带 reason 的写入才允许改写已定型的历史值
                    # （定型窗口修正后的补救性重抓走这条）。
                    revision_reason=revision_reason,
                )
                # 合并：已定型段与未变动的尾段从缓存出，回源只覆盖 [fetch_start, end]。
                # 旧实现这里只返回上游那一段，等于把缓存里的历史行从响应里丢掉。
                by_date: dict[str, dict] = {
                    r.date: {"date": r.date, "value": _coerce_value(r.value),
                             "unit": r.unit, "source_used": r.source_used}
                    for r in cached
                }
                for d, v in series.items():
                    by_date[d] = {"date": d, "value": v, "unit": concept.unit,
                                  "source_used": source}
                return {
                    "values": [by_date[d] for d in sorted(by_date)],
                    "served_from": "fetch",
                    "sources_failed": failed,
                    "stale_in_window": stale_count,
                    "cold": not cached,
                    "refetched_range": [fetch_start, end],
                }

    # 回源全部失败：退回缓存（若有）并标记降级。旧实现这里直接 return []，
    # 让"取不到数据"和"没有数据"长得一模一样。
    if cached:
        return {
            "values": [
                {"date": r.date, "value": _coerce_value(r.value), "unit": r.unit,
                 "source_used": r.source_used}
                for r in sorted(cached, key=lambda r: r.date)
            ],
            "served_from": "cache_degraded",
            "sources_failed": failed,
            "stale_in_window": stale_count,
            "cold": not cached,
        }
    return {"values": [], "served_from": "none", "sources_failed": failed,
            "stale_in_window": stale_count, "cold": not cached}


def _read_range_one(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str,
) -> list[dict]:
    """兼容入口：只要值，不要自证信息（既有调用方行为不变）。"""
    return _read_range_one_ex(session, concept_id, entity_type, entity_id,
                              start, end)["values"]
