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
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.entities.resolver import check_applicability, resolve_identifier
from fd_open_data_mcp.fetch.cache import is_stale, read_cache, write_cache
from fd_open_data_mcp.fetch.instrumentation import SourceUnavailable, instrumented_fetch
from fd_open_data_mcp.fetch.runner import FetchError, returned_columns
from fd_open_data_mcp.models import Concept, ConceptBinding, Function
from fd_open_data_mcp.ranking.scorer import rank_sources_for_concept, record_fetch_outcome
from fd_open_data_mcp.semantic.bindings import dispatch_candidates, promote_on_sample

THRESHOLD = 0.6


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


def _extract_value(result, column_name: str, date: str, source: Optional[str] = None, command: Optional[str] = None):
    """Value extraction: delegate to the adapter registry if one is registered for
    ``(source, command)`` (design.md D4); otherwise fall back to the legacy
    best-effort DataFrame lookup. Returns ``None`` when no value is found (callers
    treat ``None`` as a fetch failure and fail over)."""
    from fd_open_data_mcp.adapters import adapter_for

    if source and command:
        adapter = adapter_for(source, command)
        if adapter is not None:
            return adapter.extract_value(result, column_name, date)

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


def dispatch_one(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    date: str, requested_date: Optional[str] = None,
) -> Optional[dict]:
    """Fetch one (concept, entity, date) with cache + ranked failover."""
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
            t0 = datetime.now(timezone.utc)
            try:
                # Routed through the shared instrumentation: per-fetch proxy
                # selection + ban classification + circuit recording. Raises
                # SourceUnavailable when every proxy for this source is OPEN ->
                # fail over to the next source (real-time, per-fetch).
                result = instrumented_fetch(
                    source, fn.command, params,
                    session=session,
                    concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
                )
            except SourceUnavailable:
                # all proxies for this source are down -> next source in the ranked chain
                record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                continue
            except FetchError:
                record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                continue
            except Exception:  # noqa: BLE001 - any upstream failure -> failover
                record_fetch_outcome(session, source, concept_id, "error", _ms(t0))
                continue
            latency = _ms(t0)
            value = _extract_value(result, binding.column.name, date, source, fn.command)
            if value is None:
                record_fetch_outcome(session, source, concept_id, "error", latency)
                continue
            record_fetch_outcome(session, source, concept_id, "ok", latency)
            promote_on_sample(session, fn.id, returned_columns(result))
            write_cache(session, concept_id, entity_type, entity_id, date,
                        str(value), concept.unit, source)
            return {"date": date, "value": value, "unit": concept.unit,
                    "source_used": source, "from_cache": False}
    return None


def read(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    dates: list[str], requested_date: Optional[str] = None,
) -> list[dict]:
    """Read a concept for an entity over a list of dates (read-through + dispatch)."""
    check_applicability(session, concept_id, entity_type)
    results = []
    for d in dates:
        r = dispatch_one(session, concept_id, entity_type, entity_id, d, requested_date)
        results.append(r or {"date": d, "value": None, "error": "no source succeeded"})
    return results
