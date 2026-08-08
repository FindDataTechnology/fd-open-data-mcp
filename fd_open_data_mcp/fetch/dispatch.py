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
    read_cache_range,
    write_cache,
    write_cache_range,
)
from fd_open_data_mcp.fetch.instrumentation import SourceUnavailable, instrumented_fetch
from fd_open_data_mcp.fetch.runner import FetchError, returned_columns
from fd_open_data_mcp.models import Concept, ConceptBinding, Function
from fd_open_data_mcp.ranking.scorer import rank_sources_for_concept, record_fetch_outcome
from fd_open_data_mcp.semantic.bindings import dispatch_candidates, promote_on_sample

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
        return df.iloc[idx_str.index(date)][column_name]
    # tolerate YYYY-MM-DD vs YYYYMMDD
    compact = date.replace("-", "")
    if compact in idx_str:
        return df.iloc[idx_str.index(compact)][column_name]
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
) -> Optional[dict]:
    """Fetch one (concept, entity, date) with cache + ranked failover.

    Supports real_source-based failover: when a function declares multiple
    real_sources (e.g., eastmoney, tencent), tries each in priority order.
    """
    concept = session.get(Concept, concept_id)
    if concept is None:
        return None

    obs = read_cache(session, concept_id, entity_type, entity_id, date)
    if obs is not None and not is_stale(obs, concept.frequency):
        return {"date": date, "value": obs.value, "unit": obs.unit,
                "source_used": obs.source_used, "from_cache": True}

    for cand in rank_sources_for_concept(session, concept_id, requested_date):
        source = cand["source"]
        identifier = resolve_identifier(session, entity_type, entity_id, source)
        if identifier is None:
            continue  # graceful degradation: no per-source id for this entity
        for binding, fn in _bindings_for_source(session, concept_id, source):
            params = _build_params(fn, identifier, date, binding)

            # Get real_sources for this function (if declared)
            real_sources = _get_real_sources(fn)

            # Try each real_source in priority order (or just the library source if none declared)
            sources_to_try = real_sources if real_sources else [{"name": source, "priority": 0}]

            for real_source_spec in sources_to_try:
                real_source = real_source_spec.get("name")
                t0 = datetime.now(timezone.utc)
                try:
                    # Routed through the shared instrumentation: per-fetch proxy
                    # selection + ban classification + circuit recording. Raises
                    # SourceUnavailable when every proxy for this source is OPEN ->
                    # fail over to the next real_source or source.
                    result = instrumented_fetch(
                        source, fn.command, params,
                        real_source=real_source,  # Pass real_source for circuit tracking
                        session=session,
                        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
                    )
                except SourceUnavailable:
                    # All proxies for this real_source are down
                    if len(sources_to_try) > 1:
                        # Log failover event
                        logger.info(f"real_source failover: {real_source} -> trying next priority")
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    continue  # Try next real_source
                except FetchError:
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    continue
                except Exception:  # noqa: BLE001 - any upstream failure -> failover
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    continue

                # Success!
                latency = _ms(t0)
                value = _extract_value(result, binding.column.name, date, source, fn.command,
                                       identifier=identifier)
                if value is None:
                    record_fetch_outcome(session, source, concept_id, "error", latency)
                    continue
                record_fetch_outcome(session, source, concept_id, "ok", latency)
                promote_on_sample(session, fn.id, returned_columns(result))
                write_cache(session, concept_id, entity_type, entity_id, date,
                            str(value), concept.unit, source)
                return {"date": date, "value": value, "unit": concept.unit,
                        "source_used": source, "real_source_used": real_source, "from_cache": False}
    return None


def read(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    dates: list[str], requested_date: Optional[str] = None,
) -> list[dict]:
    """Read a concept for an entity over a list of dates (read-through + dispatch)."""
    check_applicability(session, concept_id, entity_type)
    # Defensive cap: each date may trigger a ranked network fetch; an unbounded
    # date list (e.g. 3 years of daily = ~730) can hold the MCP connection open
    # long enough to drop. Bulk history belongs in the migrate/astock_daily path,
    # not read(). Cap and warn; callers page for larger ranges.
    MAX_DATES = 366
    if len(dates) > MAX_DATES:
        logger.warning(
            "read() requested %d dates for concept %s entity %s; truncating to %d. "
            "Use the astock_daily migrate path for bulk history.",
            len(dates), concept_id, entity_id, MAX_DATES,
        )
        dates = dates[:MAX_DATES]
    results = []
    for d in dates:
        r = dispatch_one(session, concept_id, entity_type, entity_id, d, requested_date)
        results.append(r or {"date": d, "value": None, "error": "no source succeeded"})
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


def read_range(
    session: Session, concept_ids: list[int], entity_type: str, entity_id: int,
    start: str, end: str,
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
        out[concept_id] = _read_range_one(session, concept_id, entity_type, entity_id, start, end)
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


def _read_range_one(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str,
) -> list[dict]:
    concept = session.get(Concept, concept_id)
    if concept is None:
        return []
    check_applicability(session, concept_id, entity_type)

    cached = read_cache_range(session, concept_id, entity_type, entity_id, start, end)
    fresh = [r for r in cached if not is_stale(r, concept.frequency)]
    stale_count = len(cached) - len(fresh)

    if fresh and stale_count == 0:
        return [
            {"date": r.date, "value": _coerce_value(r.value), "unit": r.unit, "source_used": r.source_used}
            for r in fresh
        ]

    # Fetch path: ranked sources, one upstream range call per (source, binding).
    for cand in rank_sources_for_concept(session, concept_id, None):
        source = cand["source"]
        identifier = resolve_identifier(session, entity_type, entity_id, source)
        if identifier is None:
            continue
        for binding, fn in _bindings_for_source(session, concept_id, source):
            params = _build_range_params(fn, identifier, start, end, binding)
            real_sources = _get_real_sources(fn)
            sources_to_try = real_sources if real_sources else [{"name": source, "priority": 0}]
            for real_source_spec in sources_to_try:
                real_source = real_source_spec.get("name")
                t0 = datetime.now(timezone.utc)
                try:
                    result = instrumented_fetch(
                        source, fn.command, params,
                        real_source=real_source,
                        session=session,
                        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
                    )
                except SourceUnavailable:
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    continue
                except FetchError:
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    continue
                except Exception:  # noqa: BLE001
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    continue

                series = _extract_series(
                    result, binding.column.name, start, end, source, fn.command,
                )
                if not series:
                    record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                    continue
                record_fetch_outcome(session, source, concept_id, "ok", _ms(t0))
                promote_on_sample(session, fn.id, returned_columns(result))
                write_cache_range(
                    session, concept_id, entity_type, entity_id,
                    series, concept.unit, source,
                )
                return [
                    {"date": d, "value": v, "unit": concept.unit, "source_used": source}
                    for d, v in sorted(series.items())
                ]
    return []
