"""Transparent federated reads for registry-only indicators (design D1).

``registry_entries`` rows carry no ``concepts`` id, so the read tools cannot
serve them from the concept-keyed cache. This module routes them instead, by
the entry's ``source_db``, to the corresponding business-mcp domain read tool
(``yearbook_read`` / ``wb_read`` / ``gta_read`` / ``city_read``) over MCP
HTTP, projecting parameters and results into the standard read shape —
``{date, value, unit, source_used}`` — so the routing is invisible to the
caller beyond the provenance field.

Failure semantics (spec concept-fetch「Domain channel unavailability fails
loud」): every failure is an explicit error payload, never empty values or
silent success —

  - ``federated_read_disabled``  the rollout flag is off for this source
  - ``no_read_channel``          the source_db has no domain read tool
  - ``federation_unavailable``   business-mcp unreachable / timed out

Local concept reads never enter this module — a federation outage cannot
touch them. A short cooldown after a connection failure keeps a down
business-mcp from being hammered per-date.

Rollout flag ``FD_MCP_FEDERATED_READ``: unset/``0`` = off (default — the
a→b service dependency goes live deliberately, design risk 4); ``1``/``all``
= every mapped source; otherwise a comma-separated list of source_dbs for
the staged per-domain rollout (yearbook first).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from fd_open_data_mcp.fetch.dispatch import _coerce_value

logger = logging.getLogger(__name__)

FLAG_ENV = "FD_MCP_FEDERATED_READ"
ENDPOINT_ENV = "FD_MCP_BUSINESS_URL"
TOKEN_ENV = "FD_MCP_BUSINESS_TOKEN"
TIMEOUT_ENV = "FD_MCP_FEDERATION_TIMEOUT"
COOLDOWN_ENV = "FD_MCP_FEDERATION_COOLDOWN"
#: Yearbook multi-region correctness: 1 (default) requires an entity for
#: multi-region indicators and pages the domain read to completion; 0 restores
#: the legacy silent last-row-wins behaviour (emergency rollback only).
YEARBOOK_STRICT_ENV = "FD_MCP_YEARBOOK_REGION_STRICT"
YEARBOOK_PAGE_LIMIT = 1000
YEARBOOK_MAX_PAGES = int(os.environ.get("FD_MCP_YEARBOOK_MAX_PAGES", "20"))

_DEFAULT_TIMEOUT = 20.0
_DEFAULT_COOLDOWN = 30.0

# source_db -> business-mcp domain read tool. A source_db absent here has no
# read channel — reads report ``no_read_channel`` naming the source.
SOURCE_DB_CHANNELS: dict[str, str] = {
    "yearbook_catalog": "yearbook_read",
    "world_bank": "wb_read",
    "gta_panel": "gta_read",
    "china_city_panel": "city_read",
}

# Law/document domains are documents, not indicators: they have no place in
# the indicator read path (registry_coverage treats them the same way).


class FederationUnavailable(RuntimeError):
    """business-mcp is unreachable or timed out (fails loud, never silent)."""


# ─── Rollout flag ────────────────────────────────────────────────────────────

def federated_read_enabled(source_db: str) -> bool:
    """True when the rollout flag admits ``source_db`` (off by default)."""
    raw = os.environ.get(FLAG_ENV, "").strip()
    if not raw or raw.lower() in ("0", "false", "off", "no"):
        return False
    if raw.lower() in ("1", "true", "on", "yes", "all"):
        return True
    return source_db in {part.strip() for part in raw.split(",") if part.strip()}


# ─── Registry entry resolution ───────────────────────────────────────────────

def resolve_registry_entry(session: Session, key: Any) -> Optional[dict]:
    """Find a verified registry entry by semantic_code, then native_code.

    Fail-soft like ``registry_catalog``: a missing table (SQLite dev
    databases) yields ``None`` after one log line. Returns plain dicts —
    the registry is read-only raw SQL (no ORM model), same contract as
    ``semantic/registry_catalog``.
    """
    key_str = str(key).strip()
    if not key_str:
        return None
    try:
        row = session.execute(
            text(
                "SELECT source_db, native_code, semantic_code, name_zh, name_en, "
                "unit, frequency, domain, provenance FROM registry_entries "
                "WHERE verified AND semantic_code = :k LIMIT 1"
            ),
            {"k": key_str},
        ).mappings().first()
        if row is None:
            row = session.execute(
                text(
                    "SELECT source_db, native_code, semantic_code, name_zh, name_en, "
                    "unit, frequency, domain, provenance FROM registry_entries "
                    "WHERE verified AND native_code = :k ORDER BY id LIMIT 1"
                ),
                {"k": key_str},
            ).mappings().first()
    except (OperationalError, ProgrammingError) as exc:
        logger.info("registry table unreadable for federated read (%s)", exc)
        return None
    return dict(row) if row else None


# ─── business-mcp client (single call site, short failure cooldown) ─────────

_unavailable_until: list[float] = []  # one-slot cooldown timestamp


def _cooldown_seconds() -> float:
    raw = os.environ.get(COOLDOWN_ENV, str(_DEFAULT_COOLDOWN))
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_COOLDOWN


def _in_cooldown() -> bool:
    return bool(_unavailable_until) and time.monotonic() < _unavailable_until[0]


def _mark_unavailable() -> None:
    _unavailable_until[:] = [time.monotonic() + _cooldown_seconds()]


def reset_cooldown() -> None:
    """Test hook / operator recovery: drop the failure cooldown."""
    _unavailable_until.clear()


def _timeout() -> float:
    raw = os.environ.get(TIMEOUT_ENV, str(_DEFAULT_TIMEOUT))
    try:
        return max(1.0, float(raw))
    except ValueError:
        return _DEFAULT_TIMEOUT


def _attempt_call(url: str, tool: str, args: dict) -> dict:
    """The network hop: one MCP tool call over Streamable HTTP.

    ``FD_MCP_BUSINESS_TOKEN`` (when set) rides as the bearer credential —
    production business-mcp is JWT-gated and admits the matching shared
    service token (FDBIZ_INTERNAL_TOKEN on the far side). ``FD_MCP_FEDERATION_TIMEOUT``
    caps the whole exchange via wait_for (transport kwargs differ across
    fastmcp versions; the coroutine cancel is version-independent).
    """
    async def _call() -> dict:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        headers = {}
        token = os.environ.get(TOKEN_ENV, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        transport = StreamableHttpTransport(url, headers=headers or None)

        async with Client(transport) as client:
            result = await client.call_tool(tool, args)
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            return data
        for block in getattr(result, "content", None) or []:
            payload = getattr(block, "text", None)
            if payload:
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        raise FederationUnavailable(f"business-mcp returned no structured payload for {tool}")

    timeout = _timeout()
    try:
        return asyncio.run(asyncio.wait_for(_call(), timeout=timeout))
    except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError) as exc:
        # wait_for can surface either TimeoutError or a leaked CancelledError
        # (the inner task and the timer race at the run-loop boundary) — both
        # mean "the exchange did not complete inside the window".
        raise FederationUnavailable(
            f"business-mcp did not answer within {timeout}s ({tool})"
        ) from exc


def _call_domain_tool(tool: str, args: dict) -> dict:
    """One MCP tool call to business-mcp, behind the failure cooldown.

    Raises ``FederationUnavailable`` on endpoint misconfiguration, connection
    failure or timeout — the caller turns that into the explicit error
    payload. A tool-level error payload (e.g. ``domain_unavailable`` from the
    far side) is returned as-is; it is data, not a transport failure.
    """
    url = os.environ.get(ENDPOINT_ENV, "").strip()
    if not url:
        raise FederationUnavailable(
            f"{ENDPOINT_ENV} is not configured; the federated read channel is unknown"
        )
    if _in_cooldown():
        raise FederationUnavailable(
            "business-mcp recently unreachable (cooldown active); retry shortly"
        )
    try:
        return _attempt_call(url, tool, args)
    except FederationUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — every transport failure fails loud
        _mark_unavailable()
        logger.warning("federated read via %s failed: %s", tool, exc)
        raise FederationUnavailable(f"business-mcp unreachable: {exc}") from exc


# ─── Projection: domain payloads -> standard read rows ──────────────────────

def _years_from_dates(dates: list[str]) -> list[str]:
    """Distinct years in request order (domain panels are all annual)."""
    years: list[str] = []
    for d in dates:
        year = str(d)[:4]
        if year.isdigit() and year not in years:
            years.append(year)
    return years


def _yearbook_strict() -> bool:
    """True unless the emergency rollback flag explicitly disables strictness."""
    raw = os.environ.get(YEARBOOK_STRICT_ENV, "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def _entry_provenance(entry: dict) -> dict:
    prov = entry.get("provenance")
    if isinstance(prov, str):
        try:
            prov = json.loads(prov)
        except (TypeError, ValueError):
            prov = {}
    return prov if isinstance(prov, dict) else {}


def _payload_failure(payload: dict) -> Optional[dict]:
    """A domain payload that is an error, not data."""
    if not isinstance(payload, dict):
        return _error("federation_unavailable", "business-mcp returned no structured payload")
    if isinstance(payload.get("error"), str):
        return payload
    if isinstance(payload.get("status"), str) and payload.get("status") != "ok":
        return payload
    return None


def _yearbook_regions(entry: dict) -> tuple[list[dict], list[dict], Optional[dict]]:
    """Fetch the bounded (province, region) list and province dictionary.

    Returns ``(regions, provinces, error)``. ``provinces`` is the dictionary
    slice covering the indicator (two-letter code, name, aliases), used to
    normalise a caller's entity before pushing the filter down.
    """
    try:
        payload = _call_domain_tool(
            "yearbook_regions",
            {"indicator_id": int(entry["native_code"]), "limit": 500},
        )
    except FederationUnavailable as exc:
        return [], [], _error("federation_unavailable", str(exc),
                              source_db=entry.get("source_db"))
    failure = _payload_failure(payload)
    if failure is not None:
        return [], [], failure
    regions = [r for r in payload.get("results") or [] if isinstance(r, dict)]
    provinces = [p for p in payload.get("provinces") or [] if isinstance(p, dict)]
    return regions, provinces, None


def _province_lookup(provinces: list[dict]) -> dict[str, str]:
    """Map every accepted province label (code, name, alias) to its code."""
    lookup: dict[str, str] = {}
    for prov in provinces:
        code = prov.get("code")
        if not isinstance(code, str) or not code:
            continue
        labels = [code, prov.get("name"), *(prov.get("aliases") or [])]
        for label in labels:
            if isinstance(label, str) and label.strip():
                lookup[label.strip().lower()] = code
    return lookup


def _yearbook_args(
    entry: dict, entity: Optional[str], start_year: int, end_year: int,
) -> tuple[dict, Optional[dict]]:
    """Project yearbook read args, resolving entity -> province/region.

    Multi-region indicators without an entity return an ``entity_required``
    error (fail loud) instead of an arbitrary region. Single-region
    indicators keep the legacy no-entity behaviour. An entity matching a
    province (by code, name or alias) is pushed down as the ``province``
    filter; anything else is pushed down as ``region``.
    """
    native = int(entry["native_code"])
    base = {"indicator_id": native, "start_year": start_year, "end_year": end_year}
    if not _yearbook_strict():
        if entity:
            base["region"] = entity
        return base, None

    regions, provinces, failure = _yearbook_regions(entry)
    if failure is not None:
        return {}, failure
    region_names = {r.get("region") for r in regions if r.get("region")}
    province_lookup = _province_lookup(provinces)

    if entity:
        key = entity.strip()
        code = province_lookup.get(key.lower())
        if code:
            base["province"] = code
        else:
            # County/city name (or an unknown value — let the domain decide).
            base["region"] = key
        return base, None

    if len(region_names) <= 1:
        only = next(iter(region_names), None)
        if only:
            base["region"] = only
        return base, None

    prov = _entry_provenance(entry)
    province_codes = prov.get("province_codes") or []
    if not province_codes:
        province_codes = sorted({p.get("code") for p in provinces if p.get("code")})
    return {}, _error(
        "entity_required",
        "this yearbook indicator stores values for multiple regions; pass entity "
        "(a province or a county/city name) to select one",
        source_db=entry.get("source_db"),
        concept_id=entry.get("semantic_code") or entry.get("native_code"),
        province_codes=province_codes,
        provinces=[{"code": p.get("code"), "name": p.get("name")}
                   for p in provinces[:50]],
        regions=[{"province": r.get("province"), "region": r.get("region")}
                 for r in regions[:50]],
        region_count=len(region_names),
    )


def _yearbook_read_all(entry: dict, args: dict) -> tuple[list[dict], Optional[dict], bool]:
    """Page through yearbook_read until exhausted; (results, error, truncated)."""
    results: list[dict] = []
    offset = 0
    for _ in range(YEARBOOK_MAX_PAGES):
        page_args = dict(args, limit=YEARBOOK_PAGE_LIMIT, offset=offset)
        try:
            payload = _call_domain_tool("yearbook_read", page_args)
        except FederationUnavailable as exc:
            return [], _error("federation_unavailable", str(exc),
                              source_db=entry.get("source_db")), False
        failure = _payload_failure(payload)
        if failure is not None:
            return [], failure, False
        results.extend(payload.get("results") or [])
        if not payload.get("truncated"):
            return results, None, False
        nxt = payload.get("next_offset")
        if nxt is None:
            return results, None, True
        offset = int(nxt)
    return results, None, True


def _year_rows(
    payload: dict, source_db: str, unit: Optional[str], value_key: str = "value",
    preserve_region: bool = True,
) -> list[dict]:
    """Project a domain payload's ``results`` to standard rows.

    Yearbook rows keep their region (one row per (year, region)); the other
    channels — and the yearbook legacy rollback path — keep the one-row-per-
    year shape.
    """
    rows: list[dict] = []
    for item in payload.get("results") or []:
        year = item.get("year")
        if year is None:
            continue
        row = {
            "year": str(year),
            "value": _coerce_value(item.get(value_key)),
            "unit": item.get("unit") or unit,
        }
        if source_db == "yearbook_catalog" and preserve_region:
            row["region"] = item.get("region")
        rows.append(row)
    if source_db != "yearbook_catalog" or not preserve_region:
        by_year = {r["year"]: r for r in rows}
        return [by_year[k] for k in sorted(by_year)]
    seen: set[tuple[str, Optional[str]]] = set()
    out: list[dict] = []
    for r in sorted(rows, key=lambda x: (x["year"], (x.get("region") or "").encode("utf-8"))):
        key = (r["year"], r.get("region"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _build_args(entry: dict, entity: Optional[str], start_year: int, end_year: int) -> dict:
    """Per-channel argument projection from (native_code, entity, years)."""
    source_db = entry["source_db"]
    native = entry.get("native_code")
    if source_db == "yearbook_catalog":
        # Strict multi-region routing (province/region resolution, paging) is
        # handled by _yearbook_args/_yearbook_read_all; this fallback keeps
        # the legacy shape for the rollback flag and for callers that only
        # need the projection.
        args: dict = {"indicator_id": int(native), "start_year": start_year, "end_year": end_year}
        if entity:
            args["region"] = entity
        return args
    if source_db == "world_bank":
        if not entity:
            # wb_read has no "all countries" mode; fabricating one would be wrong.
            raise ValueError(
                "world_bank registry reads need an entity (ISO3 country code or Chinese name)"
            )
        return {"indicator_code": native, "countries": [entity],
                "start_year": start_year, "end_year": end_year}
    if source_db == "gta_panel":
        return {"variable": native, "start_year": start_year, "end_year": end_year}
    if source_db == "china_city_panel":
        args = {"variable": native, "start_year": start_year, "end_year": end_year}
        if entity:
            args["city"] = entity
        return args
    raise ValueError(f"no read channel projection for source_db {source_db!r}")


def _error(kind: str, detail: str, **extra) -> dict:
    return {"error": kind, "detail": detail, **extra}


def federated_read(
    session: Session, key: Any, dates: list[str], entity: Optional[str] = None,
) -> list[dict]:
    """Read a registry-only indicator over ``dates`` via its domain channel.

    Returns standard read rows (one per requested date). Every failure mode
    returns an explicit single-row error payload (spec: fail loud), keyed
    ``error`` — the same convention ``fetch.dispatch`` uses for dispatch
    failures.
    """
    if not dates:
        return []
    entry = resolve_registry_entry(session, key)
    if entry is None:
        return []  # not a registry indicator — caller falls back to the local path
    source_db = entry["source_db"]
    tool = SOURCE_DB_CHANNELS.get(source_db)
    if tool is None:
        return [{"date": str(dates[0]), "value": None,
                 **_error("no_read_channel",
                          f"source database {source_db!r} has no federated read channel",
                          source_db=source_db)}]
    if not federated_read_enabled(source_db):
        return [{"date": str(dates[0]), "value": None,
                 **_error("federated_read_disabled",
                          f"federated reads for {source_db!r} are disabled "
                          f"({FLAG_ENV} is off or excludes it)"),
                 "source_db": source_db}]

    years = _years_from_dates(dates)
    if not years:
        return [{"date": str(dates[0]), "value": None,
                 **_error("invalid_dates", "no parseable years in the requested dates")}]

    if source_db == "yearbook_catalog":
        return _federated_yearbook_read(entry, dates, entity)

    try:
        args = _build_args(entry, entity, int(min(years)), int(max(years)))
    except ValueError as exc:
        return [{"date": str(dates[0]), "value": None, **_error("invalid_request", str(exc))}]

    try:
        payload = _call_domain_tool(tool, args)
    except FederationUnavailable as exc:
        return [{"date": str(dates[0]), "value": None,
                 **_error("federation_unavailable", str(exc)),
                 "source_db": source_db}]

    if isinstance(payload.get("error"), str):
        # Far-side explicit error (e.g. domain_unavailable) — pass it through.
        return [{"date": str(dates[0]), "value": None, **payload}]

    by_year = {r["year"]: r for r in _year_rows(payload, source_db, entry.get("unit"))}
    return [
        {
            "date": d,
            "value": by_year.get(str(d)[:4], {}).get("value"),
            "unit": by_year.get(str(d)[:4], {}).get("unit", entry.get("unit")),
            "source_used": source_db,
        }
        for d in dates
    ]


def _federated_yearbook_read(
    entry: dict, dates: list[str], entity: Optional[str],
) -> list[dict]:
    """Yearbook read with explicit multi-region semantics.

    Strict mode: multi-region without an entity is an ``entity_required``
    error; with an entity the domain read is paged to completion and points
    carry their region. A single-region result keeps one row per date.
    """
    source_db = entry["source_db"]
    years = _years_from_dates(dates)
    strict = _yearbook_strict()
    args, failure = _yearbook_args(entry, entity, int(min(years)), int(max(years)))
    if failure is not None:
        return [{"date": str(dates[0]), "value": None, **failure}]

    if strict:
        rows, failure, truncated = _yearbook_read_all(entry, args)
    else:
        try:
            payload = _call_domain_tool("yearbook_read", args)
        except FederationUnavailable as exc:
            return [{"date": str(dates[0]), "value": None,
                     **_error("federation_unavailable", str(exc)),
                     "source_db": source_db}]
        failure = _payload_failure(payload)
        if failure is not None:
            return [{"date": str(dates[0]), "value": None, **failure}]
        rows, truncated = payload.get("results") or [], False
    if failure is not None:
        return [{"date": str(dates[0]), "value": None, **failure}]

    year_rows = _year_rows({"results": rows}, source_db, entry.get("unit"),
                           preserve_region=strict)
    wanted = {str(d)[:4] for d in dates}
    year_rows = [r for r in year_rows if r["year"] in wanted]
    regions = {r.get("region") for r in year_rows}

    if len(regions) > 1:
        out: list[dict] = []
        for d in dates:
            for r in year_rows:
                if r["year"] != str(d)[:4]:
                    continue
                out.append({"date": d, "region": r.get("region"), "value": r["value"],
                            "unit": r["unit"], "source_used": source_db})
        if truncated:
            out.append({"date": str(dates[0]), "value": None,
                        **_error("result_truncated",
                                 f"yearbook read exceeded {YEARBOOK_MAX_PAGES} pages",
                                 source_db=source_db)})
        return out

    by_year = {r["year"]: r for r in year_rows}
    out = []
    for d in dates:
        r = by_year.get(str(d)[:4])
        row = {
            "date": d,
            "value": r["value"] if r else None,
            "unit": r["unit"] if r else entry.get("unit"),
            "source_used": source_db,
        }
        if r is not None and "region" in r:
            row["region"] = r.get("region")
        out.append(row)
    if truncated:
        out.append({"date": str(dates[0]), "value": None,
                    **_error("result_truncated",
                             f"yearbook read exceeded {YEARBOOK_MAX_PAGES} pages",
                             source_db=source_db)})
    return out


def federated_read_series(
    session: Session, key: Any, start: str, end: str, entity: Optional[str] = None,
) -> dict:
    """Read a registry-only indicator's annual series over [start, end].

    Same response shape as the local ``read_series`` (points + count), with
    ``concept_id`` echoed as the registry key the caller passed and
    ``entity_type``/``entity_id`` null — registry indicators carry no local
    entity identity.
    """
    entry = resolve_registry_entry(session, key)
    if entry is None:
        return {}
    source_db = entry["source_db"]
    tool = SOURCE_DB_CHANNELS.get(source_db)
    out: dict = {
        "concept_id": str(key), "entity_type": None, "entity_id": None,
        "start": start, "end": end,
    }
    if tool is None:
        out.update(_error("no_read_channel",
                          f"source database {source_db!r} has no federated read channel",
                          source_db=source_db))
        return out
    if not federated_read_enabled(source_db):
        out.update(_error("federated_read_disabled",
                          f"federated reads for {source_db!r} are disabled "
                          f"({FLAG_ENV} is off or excludes it)"))
        return out

    start_year, end_year = str(start)[:4], str(end)[:4]
    if not (start_year.isdigit() and end_year.isdigit()):
        out.update(_error("invalid_dates", "start/end must begin with a year (YYYY...)"))
        return out

    if source_db == "yearbook_catalog":
        return _federated_yearbook_series(entry, start_year, end_year, entity, out)

    try:
        args = _build_args(entry, entity, int(start_year), int(end_year))
    except ValueError as exc:
        out.update(_error("invalid_request", str(exc)))
        return out

    try:
        payload = _call_domain_tool(tool, args)
    except FederationUnavailable as exc:
        out.update(_error("federation_unavailable", str(exc)))
        return out

    if isinstance(payload.get("error"), str):
        out.update(payload)
        return out

    rows = _year_rows(payload, source_db, entry.get("unit"))
    points = [
        {"date": r["year"], "value": r["value"], "unit": r["unit"], "source_used": source_db}
        for r in rows if start_year <= r["year"][:4] <= end_year
    ]
    out.update({"count": len(points), "points": points})
    if not points:
        out["note"] = ("the domain channel holds no values in this window — a "
                       "coverage fact, not a failure")
    return out


def _federated_yearbook_series(
    entry: dict, start_year: str, end_year: str, entity: Optional[str], out: dict,
) -> dict:
    """Yearbook series with explicit multi-region semantics (see design D5)."""
    source_db = entry["source_db"]
    strict = _yearbook_strict()
    args, failure = _yearbook_args(entry, entity, int(start_year), int(end_year))
    if failure is not None:
        out.update(failure)
        return out

    if strict:
        rows, failure, truncated = _yearbook_read_all(entry, args)
    else:
        try:
            payload = _call_domain_tool("yearbook_read", args)
        except FederationUnavailable as exc:
            out.update(_error("federation_unavailable", str(exc)))
            return out
        failure = _payload_failure(payload)
        if failure is not None:
            out.update(failure)
            return out
        rows, truncated = payload.get("results") or [], False
    if failure is not None:
        out.update(failure)
        return out

    year_rows = _year_rows({"results": rows}, source_db, entry.get("unit"),
                           preserve_region=strict)
    year_rows = [r for r in year_rows if start_year <= r["year"] <= end_year]
    regions = {r.get("region") for r in year_rows}

    if len(regions) > 1:
        series = []
        for reg in sorted(regions, key=lambda x: (x or "").encode("utf-8")):
            pts = [
                {"date": r["year"], "value": r["value"], "unit": r["unit"],
                 "source_used": source_db}
                for r in year_rows if r.get("region") == reg
            ]
            series.append({"region": reg, "points": pts})
        out.update({"count": sum(len(s["points"]) for s in series),
                    "series": series, "region_count": len(series)})
    else:
        points = [
            {"date": r["year"], "value": r["value"], "unit": r["unit"],
             "source_used": source_db, "region": r.get("region")}
            for r in year_rows
        ]
        out.update({"count": len(points), "points": points})
        if year_rows:
            out["region"] = year_rows[0].get("region")

    if truncated:
        out["truncated"] = True
        out["note"] = (f"yearbook read exceeded {YEARBOOK_MAX_PAGES} pages; "
                       "the series is incomplete")
    elif not out.get("count"):
        out["note"] = ("the domain channel holds no values in this window — a "
                       "coverage fact, not a failure")
    return out


# ─── read_via hints (design D2) ─────────────────────────────────────────────

def read_via_hint(entry: dict) -> Optional[dict]:
    """The direct-jump hint for a registry entry: which domain tool reads it.

    ``{tool, args}`` naming the business-mcp tool and the native-code
    argument that fetches the entry's values. ``None`` for sources without
    a read channel — no hint rather than a wrong one.
    """
    source_db = entry.get("source_db")
    native = entry.get("native_code")
    if native is None:
        return None
    if source_db == "yearbook_catalog":
        try:
            return {"tool": "yearbook_read", "args": {"indicator_id": int(native)}}
        except (TypeError, ValueError):
            return None
    if source_db == "world_bank":
        return {"tool": "wb_read", "args": {"indicator_code": native}}
    if source_db == "gta_panel":
        return {"tool": "gta_read", "args": {"variable": native}}
    if source_db == "china_city_panel":
        return {"tool": "city_read", "args": {"variable": native}}
    return None
