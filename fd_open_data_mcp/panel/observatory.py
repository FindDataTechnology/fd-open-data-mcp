"""Data access for the indicator observatory board (panel-indicator-observatory).

Every function reads the same tables the MCP tools serve, through the same
``get_database().get_session()`` sessions the rest of the panel uses — the
board is a view on the shared catalog, never a fork (spec crawl-control-center
「数据同源」). Three data planes:

  - ``registry_entries`` (the unified indicator registry): raw SQL, fail-soft
    exactly like ``semantic.registry_catalog`` — a missing table (local
    SQLite dev DBs, PG before the registry DDL) returns ``None``/empty after
    one log line so the board renders an explicit not-present notice instead
    of a 500.
  - the concept layer (``concept_families`` / ``concepts`` /
    ``concept_bindings`` / ``concept_mappings``): ORM models, always present.
  - coverage counts: the identical GROUP BY ``registry_coverage``
    (fd-find-data-business-mcp) runs against ``fd_open_data`` — same SQL on
    the same database is what makes the two surfaces agree at the same
    moment (spec indicator-observatory 「计数一致」).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, inspect, or_, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import (
    Concept, ConceptBinding, ConceptFamily, ConceptMapping, Function,
    FunctionColumn, Source,
)

logger = logging.getLogger(__name__)

REGISTRY_TABLE = "registry_entries"

# The registry is an ops surface: unverified entries stay visible with a
# badge (design D7) — unlike the public catalog's verified-only caliber.
_SELECT_ENTRIES_SQL = (
    "SELECT id, source_db, source_table, source_column, native_code, "
    "semantic_code, name_zh, name_en, unit, frequency, domain, verified "
    f"FROM {REGISTRY_TABLE}"
)

# Identical shape to fd-find-data-business-mcp tools_registry._COUNTS_SQL —
# the same-moment agreement scenario is satisfied by construction.
_COVERAGE_SOURCE_SQL = text(f"""
    SELECT source_db,
           count(*) AS registered,
           count(*) FILTER (WHERE verified) AS verified,
           count(*) FILTER (WHERE verified IS NOT TRUE) AS unverified
    FROM {REGISTRY_TABLE}
    GROUP BY source_db
    ORDER BY registered DESC
""")

_COVERAGE_DOMAIN_SQL = text(f"""
    SELECT domain,
           count(*) AS registered,
           count(*) FILTER (WHERE verified) AS verified,
           count(*) FILTER (WHERE verified IS NOT TRUE) AS unverified
    FROM {REGISTRY_TABLE}
    GROUP BY domain
    ORDER BY registered DESC
""")

#: Server-side page size for the registry table (57k rows must never ship
#: whole; design risk note "57k 表分页性能").
PAGE_SIZE = 50

#: Server-side caps for graph responses (design D3: neighborhood views only,
#: hard node ceiling per response).
GRAPH_MAX_NODES = 200
GRAPH_MAX_DEPTH = 2
RELATIONS_PAGE_SIZE = 50


def registry_present(session: Session) -> bool:
    """True when the registry table exists on this session's engine."""
    return inspect(session.get_bind()).has_table(REGISTRY_TABLE)


def _run_registry_sql(session: Session, sql: text, params: dict | None = None):
    """Execute registry SQL fail-soft: a missing/unreadable table yields
    ``None`` after one log line (callers render the not-present notice)."""
    try:
        if not registry_present(session):
            logger.info("panel observatory: registry table %r not present",
                        REGISTRY_TABLE)
            return None
        return session.execute(sql, params or {})
    except (OperationalError, ProgrammingError) as exc:
        logger.info("panel observatory: registry %r unreadable (%s)",
                    REGISTRY_TABLE, exc)
        return None


def filter_options(session: Session) -> dict[str, list[str]]:
    """Distinct filter values for the registry table (fail-soft empty)."""
    out: dict[str, list[str]] = {"domains": [], "source_dbs": []}
    domains = _run_registry_sql(
        session, text(f"SELECT DISTINCT domain FROM {REGISTRY_TABLE} "
                      "WHERE domain IS NOT NULL ORDER BY domain"))
    if domains is not None:
        out["domains"] = [r[0] for r in domains]
    dbs = _run_registry_sql(
        session, text(f"SELECT DISTINCT source_db FROM {REGISTRY_TABLE} "
                      "ORDER BY source_db"))
    if dbs is not None:
        out["source_dbs"] = [r[0] for r in dbs]
    return out


def entries_page(session: Session, domain: str = "", source_db: str = "",
                 verified: str = "", q: str = "", page: int = 1,
                 per_page: int = PAGE_SIZE) -> dict | None:
    """One page of registry entries with server-side filtering + paging.

    ``verified``: "" (no filter), "1" (verified only), "0" (unverified only).
    ``q``: keyword over names + codes. Returns ``{rows, total, page,
    per_page, pages}`` or ``None`` when the registry is not present.
    """
    where: list[str] = []
    params: dict[str, Any] = {}
    if domain:
        where.append("domain = :domain")
        params["domain"] = domain
    if source_db:
        where.append("source_db = :source_db")
        params["source_db"] = source_db
    if verified in ("1", "0"):
        where.append("verified IS " + ("" if verified == "1" else "NOT ") + "TRUE")
    if q:
        like = f"%{q.strip()}%"
        where.append("(name_zh LIKE :q OR name_en LIKE :q "
                     "OR semantic_code LIKE :q OR native_code LIKE :q)")
        params["q"] = like
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total_res = _run_registry_sql(
        session, text(f"SELECT count(*) FROM {REGISTRY_TABLE}{clause}"), params)
    if total_res is None:
        return None
    total = int(total_res.scalar() or 0)
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    rows_res = _run_registry_sql(
        session,
        text(_SELECT_ENTRIES_SQL + clause +
             " ORDER BY domain, semantic_code, native_code "
             "LIMIT :limit OFFSET :offset"),
        {**params, "limit": per_page, "offset": (page - 1) * per_page})
    if rows_res is None:
        return None
    return {"rows": [dict(r) for r in rows_res.mappings()],
            "total": total, "page": page, "per_page": per_page, "pages": pages}


def coverage_by_source(session: Session) -> list[dict] | None:
    """Per-source_db registered/verified/unverified — the same GROUP BY
    ``registry_coverage`` runs, so both surfaces agree at the same moment."""
    res = _run_registry_sql(session, _COVERAGE_SOURCE_SQL)
    if res is None:
        return None
    return [dict(r) for r in res.mappings()]


def coverage_by_domain(session: Session) -> list[dict] | None:
    """Per-domain registered/verified/unverified (same caliber as by-source)."""
    res = _run_registry_sql(session, _COVERAGE_DOMAIN_SQL)
    if res is None:
        return None
    return [dict(r) for r in res.mappings()]


# ── concept layer (ORM; always present) ─────────────────────────────────────

def families_overview(session: Session) -> list[dict]:
    """Families with member-concept counts and summed binding counts."""
    binding_counts = dict(
        session.query(Concept.concept_code, func.count(ConceptBinding.id))
        .join(ConceptBinding, ConceptBinding.concept_id == Concept.id)
        .group_by(Concept.concept_code).all())
    member_counts = dict(
        session.query(Concept.concept_code, func.count(Concept.id))
        .group_by(Concept.concept_code).all())
    out = []
    for f in (session.query(ConceptFamily)
              .filter_by(deprecated=False)
              .order_by(ConceptFamily.code).all()):
        out.append({
            "code": f.code, "name_zh": f.name_zh, "name_en": f.name_en,
            "description": f.description,
            "n_members": member_counts.get(f.code, 0),
            "n_bindings": binding_counts.get(f.code, 0),
        })
    return out


def family_detail(session: Session, code: str) -> dict | None:
    """A family plus its member concepts, each with its binding count."""
    f = session.query(ConceptFamily).filter_by(code=code).first()
    if f is None:
        return None
    counts = dict(
        session.query(ConceptBinding.concept_id,
                      func.count(ConceptBinding.id))
        .group_by(ConceptBinding.concept_id).all())
    members = [
        {"id": c.id, "code": c.code, "name_zh": c.name_zh,
         "name_en": c.name_en, "entity_type": c.entity_type,
         "frequency": c.frequency, "unit": c.unit, "verified": c.verified,
         "binding_count": counts.get(c.id, 0)}
        for c in (session.query(Concept)
                  .filter_by(concept_code=code, deprecated=False)
                  .order_by(Concept.code).all())]
    return {"code": f.code, "name_zh": f.name_zh, "name_en": f.name_en,
            "description": f.description, "members": members}


def _binding_rows(session: Session, concept_id: int) -> list[dict]:
    """A concept's bindings joined to their physical columns and sources —
    the native-code view operators read (spec「等价对」场景)."""
    rows = (session.query(ConceptBinding, FunctionColumn, Source)
            .join(FunctionColumn, ConceptBinding.column_id == FunctionColumn.id)
            .join(Function, FunctionColumn.function_id == Function.id)
            .join(Source, Function.source_id == Source.id)
            .filter(ConceptBinding.concept_id == concept_id)
            .all())
    return [{
        "id": b.id, "source": src.name, "source_label": src.label,
        "table": None, "column": col.name,
        "confidence": b.confidence, "provenance": b.provenance,
        "reviewed": b.reviewed,
    } for b, col, src in rows]


def concept_detail(session: Session, concept_id: int) -> dict | None:
    """An indicator's full relation surface: bindings with native codes,
    mappings, and its registry anchors (cross-source equivalence pairs)."""
    c = session.get(Concept, concept_id)
    if c is None:
        return None
    mappings = [
        {"id": m.id, "vocabulary": m.vocabulary, "term": m.term,
         "relation": m.relation, "confidence": m.confidence,
         "provenance": m.provenance, "reviewed": m.reviewed}
        for m in c.mappings]
    family = (session.query(ConceptFamily)
              .filter_by(code=c.concept_code).first() if c.concept_code else None)
    return {
        "id": c.id, "code": c.code, "name_zh": c.name_zh,
        "name_en": c.name_en, "entity_type": c.entity_type,
        "frequency": c.frequency, "unit": c.unit, "measure": c.measure,
        "verified": c.verified,
        "family_code": c.concept_code,
        "family_name": (family.name_zh or family.name_en) if family else None,
        "bindings": _binding_rows(session, c.id),
        "mappings": mappings,
        # registry anchors sharing this semantic code = cross-source
        # equivalence pairs for the indicator (spec「等价对」场景)
        "equivalences": registry_anchors(session, c.code),
    }


def registry_anchors(session: Session, semantic_code: str) -> list[dict] | None:
    """Registry rows carrying this semantic code — one per (source, native)
    anchor; together they are the indicator's cross-source equivalence set."""
    res = _run_registry_sql(
        session, text(_SELECT_ENTRIES_SQL + " WHERE semantic_code = :code "
                      "ORDER BY source_db, native_code"),
        {"code": semantic_code})
    if res is None:
        return None
    return [dict(r) for r in res.mappings()]


def mappings_page(session: Session, vocabulary: str = "", relation: str = "",
                  q: str = "", page: int = 1,
                  per_page: int = RELATIONS_PAGE_SIZE) -> dict:
    """Searchable mapping list: filter by external vocabulary, SKOS relation
    type and keyword over concept names/codes (spec「映射可检索」场景)."""
    where = []
    if vocabulary:
        where.append(ConceptMapping.vocabulary == vocabulary)
    if relation:
        where.append(ConceptMapping.relation == relation)
    kw = q.strip()
    if kw:
        like = f"%{kw}%"
        where.append(or_(Concept.code.like(like),
                         Concept.name_zh.like(like),
                         Concept.name_en.like(like),
                         ConceptMapping.term.like(like)))
    filt = [w for w in where]
    total = (session.query(func.count(ConceptMapping.id))
             .join(Concept, ConceptMapping.concept_id == Concept.id)
             .filter(*filt).scalar() or 0)
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    rows = (session.query(ConceptMapping, Concept)
            .join(Concept, ConceptMapping.concept_id == Concept.id)
            .filter(*filt)
            .order_by(ConceptMapping.vocabulary, ConceptMapping.term)
            .limit(per_page).offset((page - 1) * per_page).all())
    return {
        "rows": [{
            "id": m.id, "vocabulary": m.vocabulary, "term": m.term,
            "relation": m.relation, "confidence": m.confidence,
            "provenance": m.provenance, "reviewed": m.reviewed,
            "concept_id": c.id, "concept_code": c.code,
            "concept_name": c.name_zh or c.name_en,
        } for m, c in rows],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
    }


def mapping_vocabularies(session: Session) -> list[str]:
    return [r[0] for r in (session.query(ConceptMapping.vocabulary)
            .distinct().order_by(ConceptMapping.vocabulary).all())]


def bindings_page(session: Session, source: str = "", q: str = "",
                  page: int = 1, per_page: int = RELATIONS_PAGE_SIZE) -> dict:
    """Cross-source binding list with native codes and concepts."""
    where = []
    if source:
        where.append(Source.name == source)
    kw = q.strip()
    if kw:
        like = f"%{kw}%"
        where.append(or_(Concept.code.like(like),
                         Concept.name_zh.like(like),
                         Concept.name_en.like(like),
                         FunctionColumn.name.like(like)))
    filt = [w for w in where]
    base = (session.query(ConceptBinding)
            .join(FunctionColumn, ConceptBinding.column_id == FunctionColumn.id)
            .join(Function, FunctionColumn.function_id == Function.id)
            .join(Source, Function.source_id == Source.id)
            .join(Concept, ConceptBinding.concept_id == Concept.id))
    total = base.filter(*filt).count()
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    rows = (base.filter(*filt)
            .order_by(Source.name, FunctionColumn.name)
            .limit(per_page).offset((page - 1) * per_page).all())
    rendered = []
    for b in rows:
        col = b.column
        c = b.concept
        rendered.append({
            "id": b.id, "source": col.function.source.name,
            "column": col.name, "confidence": b.confidence,
            "provenance": b.provenance, "reviewed": b.reviewed,
            "concept_id": c.id, "concept_code": c.code,
            "concept_name": c.name_zh or c.name_en,
        })
    return {"rows": rendered, "total": total, "page": page,
            "per_page": per_page, "pages": pages}


def binding_sources(session: Session) -> list[str]:
    return [r[0] for r in (
        session.query(Source.name).distinct()
        .join(Function, Function.source_id == Source.id)
        .join(FunctionColumn, FunctionColumn.function_id == Function.id)
        .join(ConceptBinding, ConceptBinding.column_id == FunctionColumn.id)
        .order_by(Source.name).all())]


# ── relation graph (design D3: bounded neighborhood views) ──────────────────

def _node(node_id: str, kind: str, label: str, **detail) -> dict:
    return {"id": node_id, "kind": kind, "label": label, **detail}


def graph_neighborhood(session: Session, family: str = "",
                       indicator: int | None = None, domain: str = "",
                       depth: int = 1) -> dict | None:
    """Nodes/edges for one bounded graph view (design D3).

    ``family``: the family, its member concepts and — at depth 2 — their
    bindings (source columns) and mappings (external terms).
    ``indicator``: the concept's ego view — family, bindings, mappings, and
    at depth 2 the sibling concepts of its family.
    ``domain``: the registry domain — its distinct semantic codes (by anchor
    count, capped).

    Every response carries at most ``GRAPH_MAX_NODES`` nodes; a truncated
    response says so instead of lying by omission.
    """
    depth = min(max(1, depth), GRAPH_MAX_DEPTH)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add(node: dict) -> None:
        nodes.setdefault(node["id"], node)

    def edge(a: str, b: str, kind: str, label: str = "") -> None:
        edges.append({"from": a, "to": b, "kind": kind, "label": label})

    def _concept_node(c: Concept) -> dict:
        return _node(f"concept:{c.id}", "concept",
                     c.name_zh or c.name_en or c.code,
                     code=c.code, name_zh=c.name_zh, name_en=c.name_en,
                     entity_type=c.entity_type, frequency=c.frequency,
                     unit=c.unit, verified=c.verified,
                     binding_count=len(c.bindings),
                     mapping_count=len(c.mappings))

    def _attach_relations(c: Concept) -> None:
        for b in c.bindings:
            src = b.column.function.source
            add(_node(f"column:{b.column_id}", "column",
                      f"{src.name}.{b.column.name}",
                      source=src.name, column=b.column.name,
                      native_code=b.column.name,
                      confidence=b.confidence, provenance=b.provenance))
            edge(f"concept:{c.id}", f"column:{b.column_id}", "binding")
        for m in c.mappings:
            add(_node(f"term:{m.id}", "term", f"{m.vocabulary}:{m.term}",
                      vocabulary=m.vocabulary, term=m.term, relation=m.relation,
                      confidence=m.confidence, provenance=m.provenance))
            edge(f"concept:{c.id}", f"term:{m.id}", "mapping", m.relation)

    if indicator is not None:
        c = session.get(Concept, indicator)
        if c is None:
            return None
        add(_concept_node(c))
        if c.concept_code:
            f = session.query(ConceptFamily).filter_by(
                code=c.concept_code).first()
            if f is not None:
                add(_node(f"family:{f.code}", "family",
                          f.name_zh or f.name_en or f.code,
                          code=f.code, name_zh=f.name_zh, name_en=f.name_en))
                edge(f"family:{f.code}", f"concept:{c.id}", "member")
        _attach_relations(c)
        if depth >= 2 and c.concept_code:
            for sib in (session.query(Concept)
                        .filter(Concept.concept_code == c.concept_code,
                                Concept.id != c.id,
                                Concept.deprecated.is_(False))
                        .limit(GRAPH_MAX_NODES).all()):
                add(_concept_node(sib))
                edge(f"family:{c.concept_code}", f"concept:{sib.id}", "member")
                if len(nodes) >= GRAPH_MAX_NODES:
                    break
    elif family:
        f = session.query(ConceptFamily).filter_by(code=family).first()
        if f is None:
            return None
        add(_node(f"family:{f.code}", "family",
                  f.name_zh or f.name_en or f.code,
                  code=f.code, name_zh=f.name_zh, name_en=f.name_en,
                  description=f.description))
        members = (session.query(Concept)
                   .filter(Concept.concept_code == f.code,
                           Concept.deprecated.is_(False))
                   .limit(GRAPH_MAX_NODES).all())
        for c in members:
            add(_concept_node(c))
            edge(f"family:{f.code}", f"concept:{c.id}", "member")
            if depth >= 2:
                _attach_relations(c)
            if len(nodes) >= GRAPH_MAX_NODES:
                break
    elif domain:
        res = _run_registry_sql(
            session, text(
                f"SELECT semantic_code, count(*) AS n, "
                "max(name_zh) AS name_zh, max(name_en) AS name_en, "
                # max not bool_or: same any-true semantics, portable to the
                # SQLite dev/test backend (bool_or is PostgreSQL-only)
                "max(verified) AS verified "
                f"FROM {REGISTRY_TABLE} WHERE domain = :domain "
                "AND semantic_code IS NOT NULL "
                "GROUP BY semantic_code ORDER BY n DESC "
                "LIMIT :limit"),
            {"domain": domain, "limit": GRAPH_MAX_NODES})
        if res is None:
            return None
        add(_node(f"domain:{domain}", "domain", domain, domain=domain))
        for r in res.mappings():
            sc = r["semantic_code"]
            add(_node(f"indicator:{sc}", "indicator",
                      r["name_zh"] or r["name_en"] or sc,
                      semantic_code=sc, name_zh=r["name_zh"],
                      name_en=r["name_en"], verified=bool(r["verified"]),
                      anchors=int(r["n"])))
            edge(f"domain:{domain}", f"indicator:{sc}", "domain")
    else:
        return None

    truncated = len(nodes) >= GRAPH_MAX_NODES
    node_list = list(nodes.values())[:GRAPH_MAX_NODES]
    keep = {n["id"] for n in node_list}
    edge_list = [e for e in edges if e["from"] in keep and e["to"] in keep]
    return {"nodes": node_list, "edges": edge_list,
            "truncated": truncated, "max_nodes": GRAPH_MAX_NODES,
            "depth": depth}
