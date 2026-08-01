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


def check_applicability(session: Session, concept_id: int, entity_type: str) -> Concept:
    """Return the concept if it applies to entity_type; raise EntityTypeMismatch otherwise."""
    c = session.get(Concept, concept_id)
    if c is None:
        raise ValueError(f"concept {concept_id} not found")
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
    """Map an A/HK/US stock code to its yfinance symbol."""
    ex = (exchange or "").upper()
    if "SH" in ex or ex == "SSE" or (market == "CN" and code.startswith("6")):
        return f"{code}.SS"
    if "SZ" in ex or ex == "SZSE" or (market == "CN" and code.startswith(("0", "3"))):
        return f"{code}.SZ"
    if "HK" in ex or market == "HK":
        return f"{code}.HK"
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
    """Seed akshare (code as-is) + yfinance (code + exchange suffix) for all symbols."""
    from fd_open_data_mcp.entities.taxonomy import list_entities

    stocks = list_entities("stock", db_path)
    rows: list[dict] = []
    for st in stocks:
        code = st.get("code")
        if not code:
            continue
        eid = st["id"]
        rows.append({"entity_type": "stock", "entity_id": eid, "source": "akshare", "identifier": code})
        yf_sym = _yfinance_symbol(code, st.get("exchange"), st.get("market"))
        rows.append({"entity_type": "stock", "entity_id": eid, "source": "yfinance", "identifier": yf_sym})
        if (st.get("market") or "").upper() == "US":
            rows.append({"entity_type": "stock", "entity_id": eid, "source": "edgar", "identifier": code})
    _bulk_upsert_identifiers(session, rows)
    return {
        "akshare": sum(1 for r in rows if r["source"] == "akshare"),
        "yfinance": sum(1 for r in rows if r["source"] == "yfinance"),
        "edgar": sum(1 for r in rows if r["source"] == "edgar"),
    }


def seed_country_identifiers(session: Session, db_path: Optional[str] = None) -> dict:
    """Seed worldbank identifiers (= iso_code) and wbgapi identifiers (= iso3) for all countries."""
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
    _bulk_upsert_identifiers(session, rows)
    return {
        "worldbank": sum(1 for r in rows if r["source"] == "worldbank"),
        "wbgapi": sum(1 for r in rows if r["source"] == "wbgapi"),
    }
