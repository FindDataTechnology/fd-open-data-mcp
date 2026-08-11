"""Cross-source entity identifier resolution + applicability guard.

- ``check_applicability`` rejects a request whose entity_type doesn't match
  the concept's entity_type (spec entity-identity).
- ``resolve_identifier`` returns the per-source identifier for an entity, or
  None - callers skip that source on None (graceful degradation, design.md).
- ``seed_*`` populate entity_source_identifiers for the common mappings.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Concept, EntitySourceIdentifier


class EntityTypeMismatch(Exception):
    """Raised when a concept is requested for an incompatible entity type."""


class ConceptDeprecated(Exception):
    """Raised when a deprecated concept is requested for dispatch.

    Carries the canonical replacement (if any) so callers can surface it to the
    user (spec ``concept-canonicalization``).
    """

    def __init__(self, concept, replacement=None):
        self.concept = concept
        self.replacement = replacement
        msg = f"concept {concept.code} (id={concept.id}) is deprecated"
        if replacement is not None:
            msg += f"; use canonical replacement {replacement.code} (id={replacement.id})"
        super().__init__(msg)


def find_canonical_replacement(session: Session, concept: Concept):
    """Find the non-deprecated canonical concept for a deprecated one (by name_zh).

    Prefers a non-``symbol`` entity_type so a deprecated ``PRICE_CLOSE`` (symbol)
    resolves to ``price.close`` (stock) rather than another symbol-typed twin.
    """
    if not concept.name_zh:
        return None
    return (
        session.query(Concept)
        .filter(
            Concept.name_zh == concept.name_zh,
            Concept.deprecated.is_(False),
            Concept.id != concept.id,
        )
        .order_by(Concept.entity_type == "symbol")  # non-symbol first
        .first()
    )


def check_applicability(session: Session, concept_id: int, entity_type: str) -> Concept:
    """Return the concept if it applies to entity_type; raise on mismatch or deprecation."""
    c = session.get(Concept, concept_id)
    if c is None:
        raise ValueError(f"concept {concept_id} not found")
    if c.deprecated:
        raise ConceptDeprecated(c, find_canonical_replacement(session, c))
    if c.entity_type != entity_type:
        raise EntityTypeMismatch(
            f"concept {c.code} applies to entity_type={c.entity_type}, not {entity_type}"
        )
    return c


def resolve_identifier(
    session: Session, entity_type: str, entity_id: int, source: str,
) -> Optional[str]:
    """Return the per-source identifier for an entity, or None if none registered."""
    row = session.query(EntitySourceIdentifier).filter_by(
        entity_type=entity_type, entity_id=entity_id, source=source,
    ).first()
    return row.identifier if row else None


def add_identifier(
    session: Session, entity_type: str, entity_id: int, source: str, identifier: str,
) -> EntitySourceIdentifier:
    """Upsert a per-source entity identifier."""
    row = session.query(EntitySourceIdentifier).filter_by(
        entity_type=entity_type, entity_id=entity_id, source=source,
    ).first()
    if row:
        row.identifier = identifier
    else:
        row = EntitySourceIdentifier(
            entity_type=entity_type, entity_id=entity_id, source=source, identifier=identifier,
        )
        session.add(row)
        session.flush()
    session.commit()
    return row


_ISO2_TO_ISO3: dict[str, str] = {
    "CN": "CHN", "US": "USA", "JP": "JPN", "KR": "KOR", "DE": "DEU", "FR": "FRA",
    "GB": "GBR", "IN": "IND", "BR": "BRA", "RU": "RUS", "CA": "CAN", "AU": "AUS",
    "IT": "ITA", "ES": "ESP", "MX": "MEX", "ID": "IDN", "TR": "TUR", "SA": "SAU",
    "AR": "ARG", "ZA": "ZAF", "EG": "EGY", "NG": "NGA", "NL": "NLD", "CH": "CHE",
    "SE": "SWE", "PL": "POL", "BE": "BEL", "TH": "THA", "MY": "MYS", "SG": "SGP",
    "PH": "PHL", "VN": "VNM", "HK": "HKG", "TW": "TWN",
}


def _iso2_to_iso3(code: Optional[str]) -> Optional[str]:
    return _ISO2_TO_ISO3.get((code or "").upper())


def _yfinance_symbol(code: str, exchange: Optional[str], market: Optional[str]) -> str:
    """Map an A/HK/US stock code to its yfinance symbol.

    A-share suffix is derived from the code prefix (spec ``stock-source-identity``):
    ``60xxxx`` -> ``.SS`` (Shanghai), ``00xxxx``/``30xxxx`` -> ``.SZ`` (Shenzhen).
    The exchange metadata is often the ambiguous ``"SSE/SZSE"`` with no ``market``
    field, so the prefix is authoritative. HK is checked first because HK codes
    (e.g. ``03988``) share leading digits with Shenzhen codes.
    """
    ex = (exchange or "").upper()
    mk = (market or "").upper()
    if "HK" in ex or mk == "HK":
        return f"{code}.HK"
    if code.startswith("6"):
        return f"{code}.SS"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    return code  # US / unknown: use as-is


def _bulk_upsert_identifiers(session: Session, rows: list[dict]) -> None:
    """Bulk upsert identifier rows (entity_type, entity_id, source, identifier).

    Uses psycopg2 ``execute_values`` for a true single-statement bulk insert when on
    Postgres (essential for tens of thousands of rows over a remote connection);
    falls back to per-row executemany on other dialects (e.g. SQLite in tests).
    """
    if not rows:
        return
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    data = [(r["entity_type"], r["entity_id"], r["source"], r["identifier"], now) for r in rows]
    sql = """
        INSERT INTO entity_source_identifiers (entity_type, entity_id, source, identifier, created_at)
        VALUES %s
        ON CONFLICT (entity_type, entity_id, source) DO UPDATE SET identifier = EXCLUDED.identifier
    """
    raw = session.connection().connection  # underlying DBAPI connection
    is_postgres = type(raw).__module__.startswith("psycopg2")
    try:
        if is_postgres:
            from psycopg2.extras import execute_values
            cur = raw.cursor()
            try:
                execute_values(cur, sql, data)
            finally:
                cur.close()
        else:
            # sqlite / other dialects (tests): SQLAlchemy executemany with :param placeholders.
            # SQLite >= 3.24 supports ON CONFLICT ... DO UPDATE SET ... = EXCLUDED.<col>.
            from sqlalchemy import text
            session.execute(text(sql.replace("%s", "(:et, :eid, :src, :id, :ts)")),
                            [{"et": r[0], "eid": r[1], "src": r[2], "id": r[3], "ts": r[4]} for r in data])
    except ImportError:
        from sqlalchemy import text
        session.execute(text(sql.replace("%s", "(:et, :eid, :src, :id, :ts)")),
                        [{"et": r[0], "eid": r[1], "src": r[2], "id": r[3], "ts": r[4]} for r in data])
    session.commit()


def seed_stock_identifiers(session: Session, db_path: Optional[str] = None) -> dict:
    """Seed akshare (code as-is) + yfinance (code + exchange suffix) for all stock entities.

    Reads the ontology ``entities`` table (entity_type='stock') so that every
    stock - not just the taxonomy sample - gets a correct per-source identifier
    (spec ``stock-source-identity``). The ``db_path`` arg is retained for
    signature compatibility but unused; the session is the ontology session.
    """
    from fd_open_data_mcp.models import Entity

    stocks = session.query(Entity).filter(Entity.entity_type == "stock").all()
    rows: list[dict] = []
    for st in stocks:
        code = st.code
        if not code:
            continue
        eid = st.id
        meta = st.metadata_json or {}
        rows.append({"entity_type": "stock", "entity_id": eid, "source": "akshare", "identifier": code})
        yf_sym = _yfinance_symbol(code, meta.get("exchange"), meta.get("market"))
        rows.append({"entity_type": "stock", "entity_id": eid, "source": "yfinance", "identifier": yf_sym})
        if (meta.get("market") or "").upper() == "US":
            rows.append({"entity_type": "stock", "entity_id": eid, "source": "edgar", "identifier": code})
    _bulk_upsert_identifiers(session, rows)
    return {
        "akshare": sum(1 for r in rows if r["source"] == "akshare"),
        "yfinance": sum(1 for r in rows if r["source"] == "yfinance"),
        "edgar": sum(1 for r in rows if r["source"] == "edgar"),
    }


def seed_country_identifiers(session: Session, db_path: Optional[str] = None) -> dict:
    """Seed per-source identifiers for all countries.

    worldbank = ISO2 ``iso_code``; wbgapi = ISO3; datacommons = ``country/<ISO3>``
    DCID (e.g. ``country/USA``), per Data Commons entity conventions.
    """
    from fd_open_data_mcp.entities.taxonomy import list_entities

    countries = list_entities("country", db_path)
    rows: list[dict] = []
    for c in countries:
        iso = c.get("iso_code")
        if not iso:
            continue
        rows.append({"entity_type": "country", "entity_id": c["id"], "source": "worldbank", "identifier": iso})
        iso3 = _iso2_to_iso3(iso)
        if iso3:
            rows.append({"entity_type": "country", "entity_id": c["id"], "source": "wbgapi", "identifier": iso3})
            rows.append({"entity_type": "country", "entity_id": c["id"], "source": "datacommons", "identifier": f"country/{iso3}"})
    _bulk_upsert_identifiers(session, rows)
    return {
        "worldbank": sum(1 for r in rows if r["source"] == "worldbank"),
        "wbgapi": sum(1 for r in rows if r["source"] == "wbgapi"),
        "datacommons": sum(1 for r in rows if r["source"] == "datacommons"),
    }


def repair_stock_identifiers(session: Session, apply: bool = False) -> dict:
    """Report and optionally fix stock source-identifier drift.

    A stock's canonical ``akshare`` identifier is its 6-digit ``code``;
    ``yfinance`` is the code with the ``.SS``/``.SZ``/``.HK`` suffix
    (spec ``stock-source-identity``). Dry-run (``apply=False``) lists every
    drift row; ``apply=True`` re-seeds canonical ``akshare`` + ``yfinance``
    for all stock entities via the idempotent bulk upsert.

    ``cn-report`` (and any non-akshare/yfinance source) identifiers are never
    touched - the re-seed only writes ``akshare`` and ``yfinance`` rows.
    """
    from fd_open_data_mcp.models import Entity

    stocks = session.query(Entity).filter(Entity.entity_type == "stock").all()
    canonical: dict[tuple[int, str], str] = {}
    code_by_id: dict[int, str] = {}
    for st in stocks:
        code = st.code
        if not code:
            continue
        eid = st.id
        meta = st.metadata_json or {}
        code_by_id[eid] = code
        canonical[(eid, "akshare")] = code
        canonical[(eid, "yfinance")] = _yfinance_symbol(code, meta.get("exchange"), meta.get("market"))

    existing: dict[tuple[int, str], str] = {
        (r.entity_id, r.source): r.identifier
        for r in session.query(EntitySourceIdentifier).filter(
            EntitySourceIdentifier.entity_type == "stock",
            EntitySourceIdentifier.source.in_(["akshare", "yfinance"]),
        )
    }

    drift: list[dict] = []
    for (eid, source), canon in canonical.items():
        stored = existing.get((eid, source))
        if stored != canon:
            drift.append({
                "entity_id": eid,
                "code": code_by_id.get(eid),
                "source": source,
                "stored": stored,
                "canonical": canon,
            })

    if apply:
        counts = seed_stock_identifiers(session)
        return {"applied": True, "drift_found": len(drift), "reseed": counts}

    return {
        "applied": False,
        "drift_found": len(drift),
        "drift_total": len(drift),
        "drift_sample": drift[:50],
    }


def deprecation_map(session: Session) -> dict:
    """Return the deprecated -> canonical concept alias record.

    Lists every deprecated concept and its canonical replacement (resolved by
    name_zh via ``find_canonical_replacement``). Used by the
    ``deprecation-map`` CLI as the authoritative alias record for callers
    migrating off the deprecated ``symbol`` stock-concept codes.
    """
    deprecated = session.query(Concept).filter(Concept.deprecated.is_(True)).all()
    rows = []
    for c in deprecated:
        repl = find_canonical_replacement(session, c)
        rows.append({
            "deprecated_id": c.id,
            "deprecated_code": c.code,
            "entity_type": c.entity_type,
            "name_zh": c.name_zh,
            "category": c.category,
            "canonical_id": repl.id if repl else None,
            "canonical_code": repl.code if repl else None,
        })
    return {"deprecated_count": len(rows), "with_replacement": sum(1 for r in rows if r["canonical_id"]), "map": rows}
