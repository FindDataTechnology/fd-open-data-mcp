"""Catalog importer: read providers, upsert into the ontology catalog, report drift.

Idempotent: re-running upserts (no duplicates) and reports the added/removed
curated-function sets vs the previous import. Upstream-only callables are
imported as ``verified=False`` and excluded from concept-binding dispatch.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.catalog.enrich import derive_frequency, derive_meaning
from fd_open_data_mcp.catalog.providers import PROVIDERS, provider_names
from fd_open_data_mcp.catalog.readers import read_provider
from fd_open_data_mcp.catalog.upstream import introspect_upstream
from fd_open_data_mcp.models import Function, FunctionColumn, Source


def _upsert_source(session: Session, name: str, cfg: dict) -> Source:
    src = session.query(Source).filter_by(name=name).first()
    if src is None:
        src = Source(name=name, label=cfg.get("label", name), url=cfg.get("source_url"))
        session.add(src)
        session.flush()
    else:
        src.label = cfg.get("label", name)
        src.url = cfg.get("source_url")
    return src


def _normalize_params(params) -> list:
    """Ensure parameters is a list of dicts (parse JSON text if needed)."""
    if params is None:
        return []
    if isinstance(params, str):
        import json
        try:
            params = json.loads(params)
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(params, list):
        return []
    return params


def _upsert_function(
    session: Session, source_id: int, rec: dict, verified: bool, scanner_mode: str
) -> Function:
    cmd = rec["command"]
    fn = session.query(Function).filter_by(source_id=source_id, command=cmd).first()
    freq = derive_frequency(rec.get("category"), rec.get("description"))
    params = _normalize_params(rec.get("parameters"))
    if fn is None:
        fn = Function(
            source_id=source_id, command=cmd, category=rec.get("category"),
            description=rec.get("description"), parameters=params,
            verified=verified, scanner_mode=scanner_mode, frequency=freq,
        )
        session.add(fn)
        session.flush()
    else:
        fn.category = rec.get("category")
        fn.description = rec.get("description")
        fn.parameters = params
        fn.verified = verified
        fn.scanner_mode = scanner_mode
        fn.frequency = freq

    existing_cols = {c.name: c for c in fn.columns}
    seen_names: set[str] = set()
    for col in rec.get("columns") or []:
        cname = col.get("name")
        # Skip empty or duplicate names within the same function. Some source
        # registries (e.g. akshare) emit duplicate column names per function;
        # keep the first occurrence and drop the rest.
        if not cname or cname in seen_names:
            continue
        seen_names.add(cname)
        meaning = derive_meaning(col.get("description"))
        if cname in existing_cols:
            c = existing_cols[cname]
            c.type = col.get("type")
            c.description = col.get("description")
            c.meaning = meaning
            c.semantic_type = col.get("semantic_type")
        else:
            session.add(FunctionColumn(
                function_id=fn.id, name=cname, type=col.get("type"),
                description=col.get("description"), meaning=meaning,
                semantic_type=col.get("semantic_type"),
            ))
    return fn


def _import_upstream_extras(
    session: Session, source_id: int, cfg: dict, curated_commands: set[str]
) -> int:
    upstream = cfg.get("upstream")
    if not upstream:
        return 0
    try:
        recs = introspect_upstream(upstream)
    except Exception:  # noqa: BLE001 - upstream pkg not installed; skip silently
        return 0
    count = 0
    for rec in recs:
        if rec["command"] in curated_commands:
            continue
        _upsert_function(session, source_id, rec, verified=False, scanner_mode="upstream-curated")
        count += 1
    return count


def import_provider(provider_name: str, session: Optional[Session] = None) -> dict:
    """Import one provider's registry into the catalog. Returns a drift report."""
    if provider_name not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider_name}")
    from fd_open_data_mcp.db import get_database

    own_session = session is None
    if own_session:
        session = get_database().get_session()
    try:
        cfg = PROVIDERS[provider_name]
        source = _upsert_source(session, provider_name, cfg)

        old_curated = {
            f.command for f in session.query(Function).filter_by(source_id=source.id, verified=True)
        }
        records, errors = read_provider(provider_name)
        new_curated: set[str] = set()
        for rec in records:
            _upsert_function(session, source.id, rec, verified=True, scanner_mode=cfg["scanner_mode"])
            new_curated.add(rec["command"])

        unverified_count = _import_upstream_extras(session, source.id, cfg, new_curated)
        session.commit()

        return {
            "provider": provider_name,
            "curated_count": len(new_curated),
            "unverified_count": unverified_count,
            "added": sorted(new_curated - old_curated),
            "removed": sorted(old_curated - new_curated),
            "errors": errors,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        if own_session:
            session.close()


def import_all(session: Optional[Session] = None) -> list[dict]:
    """Import all configured providers + auto-discover manifests. Returns per-provider reports."""
    own_session = session is None
    if own_session:
        from fd_open_data_mcp.db import get_database
        session = get_database().get_session()
    try:
        reports = [import_provider(name, session) for name in provider_names()]
        # auto-discover manifests from entry points + datasources/ dir
        from fd_open_data_mcp.catalog.register import discover_datasources
        discovered = discover_datasources(session)
        if discovered:
            reports.append({"discovered": discovered})
        return reports
    finally:
        if own_session:
            session.close()
