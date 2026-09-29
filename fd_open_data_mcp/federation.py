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
                "unit, frequency, domain FROM registry_entries "
                "WHERE verified AND semantic_code = :k LIMIT 1"
            ),
            {"k": key_str},
        ).mappings().first()
        if row is None:
            row = session.execute(
                text(
                    "SELECT source_db, native_code, semantic_code, name_zh, name_en, "
                    "unit, frequency, domain FROM registry_entries "
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
    service token (FDBIZ_INTERNAL_TOKEN on the far side).
    """
    async def _call() -> dict:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        headers = {}
        token = os.environ.get(TOKEN_ENV, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        transport = StreamableHttpTransport(url, headers=headers or None, timeout=_timeout())
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

    return asyncio.run(_call())


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


def _year_rows(
    payload: dict, source_db: str, unit: Optional[str], value_key: str = "value",
) -> list[dict]:
    """Project a domain payload's ``results`` to [{year, value, unit}] rows."""
    rows: dict[str, dict] = {}
    for item in payload.get("results") or []:
        year = item.get("year")
        if year is None:
            continue
        rows[str(year)] = {
            "year": str(year),
            "value": _coerce_value(item.get(value_key)),
            "unit": item.get("unit") or unit,
        }
    return [rows[k] for k in sorted(rows)]


def _build_args(entry: dict, entity: Optional[str], start_year: int, end_year: int) -> dict:
    """Per-channel argument projection from (native_code, entity, years)."""
    source_db = entry["source_db"]
    native = entry.get("native_code")
    if source_db == "yearbook_catalog":
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
