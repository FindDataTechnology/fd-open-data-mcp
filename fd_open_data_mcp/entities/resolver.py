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


def seed_stock_identifiers(session: Session, db_path: Optional[str] = None) -> dict:
    """Seed akshare (code as-is) + yfinance (code + exchange suffix) for all symbols."""
    from fd_open_data_mcp.entities.taxonomy import list_entities

    stocks = list_entities("stock", db_path)
    ak = yf = ed = 0
    for st in stocks:
        code = st.get("code")
        if not code:
            continue
        eid = st["id"]
        add_identifier(session, "stock", eid, "akshare", code)
        ak += 1
        yf_sym = _yfinance_symbol(code, st.get("exchange"), st.get("market"))
        add_identifier(session, "stock", eid, "yfinance", yf_sym)
        yf += 1
        if (st.get("market") or "").upper() == "US":
            add_identifier(session, "stock", eid, "edgar", code)
            ed += 1
    return {"akshare": ak, "yfinance": yf, "edgar": ed}


def seed_country_identifiers(session: Session, db_path: Optional[str] = None) -> dict:
    """Seed worldbank identifiers (= iso_code) and wbgapi identifiers (= iso3) for all countries."""
    from fd_open_data_mcp.entities.taxonomy import list_entities

    countries = list_entities("country", db_path)
    wb = wg = 0
    for c in countries:
        iso = c.get("iso_code")
        if not iso:
            continue
        add_identifier(session, "country", c["id"], "worldbank", iso)
        wb += 1
        iso3 = _iso2_to_iso3(iso)
        if iso3:
            add_identifier(session, "country", c["id"], "wbgapi", iso3)
            wg += 1
    return {"worldbank": wb, "wbgapi": wg}
