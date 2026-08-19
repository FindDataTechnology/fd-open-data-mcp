"""Entity taxonomy lookup against live Postgres (guangzhou-xinru).

Entities are stored in the live ontology database at guangzhou-xinru:30432. This module
looks up an entity by type + code and returns its row (including id), so that
``entity_source_identifiers`` can reference it by ``(entity_type, entity_id)``.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from fd_open_data_mcp.catalog.providers import finddata_root


# Connection string for the remote ontology DB
PG_HOST = os.environ.get("PG_HOST", "guangzhou-xinru")
PG_PORT = int(os.environ.get("PG_PORT", 30432))
PG_USER = os.environ.get("PG_USER", "fd")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
PG_DATABASE = os.environ.get("PG_DATABASE", "fd_open_data")

DATABASE_URL = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"


def default_engine():
    """Create a connection engine for the remote Postgres DB."""
    return create_engine(DATABASE_URL)


# Canonical entity_type vocabulary. Mirrors the tables on the remote DB:
# - countries (iso_code) → country
# - cities (code) → city
# - symbols (ticker + symbol_type) → stock/fund/bond/coin/index/future
# - companies (code) → company (with sector as industry classification)
# - person → logical-only (no taxonomy table); used for fund managers
#
# Note: `fund` is the single canonical entity_type for ALL fund subtypes.
# Fund classification (open/etf/lof/money/graded) lives in
# `entities.metadata_json.subtype`, NOT in entity_type — `etf` is rejected
# as an entity_type (see validate_entity_type).
ENTITY_TYPES: tuple[str, ...] = (
    "country", "city", "stock", "fund", "bond", "index", "future", "crypto",
    "organization", "industry", "exchange", "company", "person"
)

# entity_type values that are explicitly rejected, with the redirect message.
REJECTED_ENTITY_TYPES: dict[str, str] = {
    "etf": "use entity_type='fund' with metadata_json.subtype='etf'",
}


def validate_entity_type(entity_type: str) -> str:
    """Reject removed/invalid entity_type values; return the value if acceptable.

    `etf` was folded into `fund` (subtype in metadata_json) — callers attempting
    to create entities or concepts with entity_type='etf' get a directed error.
    Types outside ENTITY_TYPES are still allowed (logical-only types such as
    `organization`/`person` have no taxonomy table by design); only explicitly
    rejected values raise.
    """
    if entity_type in REJECTED_ENTITY_TYPES:
        raise ValueError(
            f"entity_type={entity_type!r} is not accepted: {REJECTED_ENTITY_TYPES[entity_type]}"
        )
    return entity_type

# Entity type mappings to (table, code_column, name_columns, optional columns)
# stock = symbol where symbol_type='stock', fund = 'etf' or 'fund', etc.
ENTITY_TABLES: dict[str, tuple[str, str, list[str], Optional[dict]]] = {
    "country": ("countries", "iso_code", ["name_en", "name_zh"], {"region": True}),
    "city": ("cities", "code", ["name_en", "name_zh"], {
        "country_iso": True, "latitude": True, "longitude": True
    }),
    "company": ("companies", "code", ["name_en", "name_zh"], {"sector": True}),
    # stock/fund/bond/etc. all map to `symbols` table with different symbol_type filters
}

# Symbol type mapping: which symbol_type values correspond to each entity type
SYMBOL_TYPE_MAP: dict[str, list[str]] = {
    "stock": ["stock"],
    "fund": ["etf", "fund"],
    "bond": ["bond"],
    "index": ["index"],
    "future": ["future"],
    "crypto": ["coin"],  # crypto uses "coin" in symbol_type
    # option is excluded (not commonly used for data crawling)
}


def list_entities(entity_type: str, limit: int = 1000) -> list[dict]:
    """Return all entities of a type as dicts. Uses SQLAlchemy for PostgreSQL."""
    engine = default_engine()
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        if entity_type == "country":
            rows = session.execute(
                text("SELECT id, iso_code, name_en, name_zh, region FROM countries ORDER BY iso_code LIMIT :limit"),
                {"limit": limit}
            ).fetchall()
            return [{"id": r.id, "iso_code": r.iso_code, "name_en": r.name_en, "name_zh": r.name_zh,
                     "region": r.region} for r in rows]

        elif entity_type == "city":
            rows = session.execute(
                text("""SELECT id, code, name_en, name_zh, country_iso, latitude, longitude
                        FROM cities ORDER BY code LIMIT :limit"""),
                {"limit": limit}
            ).fetchall()
            return [{"id": r.id, "code": r.code, "name_en": r.name_en, "name_zh": r.name_zh,
                     "country_iso": r.country_iso, "latitude": r.latitude, "longitude": r.longitude} for r in rows]

        elif entity_type == "company":
            rows = session.execute(
                text("""SELECT id, code, name_en, name_zh, sector FROM companies ORDER BY code LIMIT :limit"""),
                {"limit": limit}
            ).fetchall()
            return [{"id": r.id, "code": r.code, "name_en": r.name_en, "name_zh": r.name_zh,
                     "sector": r.sector} for r in rows]

        elif entity_type in SYMBOL_TYPE_MAP:
            symbol_types = SYMBOL_TYPE_MAP[entity_type]
            types_str = ", ".join([f"'{t}'" for t in symbol_types])
            rows = session.execute(
                text(f"""SELECT id, ticker, symbol_type, name_en, name_zh, exchange, company_code
                        FROM symbols WHERE symbol_type IN ({types_str}) ORDER BY ticker LIMIT :limit"""),
                {"limit": limit}
            ).fetchall()
            return [{"id": r.id, "ticker": r.ticker, "symbol_type": r.symbol_type, "name_en": r.name_en,
                     "name_zh": r.name_zh, "exchange": r.exchange, "company_code": r.company_code} for r in rows]

        else:
            return []
    finally:
        session.close()


def find_entity(entity_type: str, code: str) -> Optional[dict]:
    """Look up one entity by its code column. Returns the row dict (with id) or None."""
    engine = default_engine()
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        if entity_type == "country":
            row = session.execute(
                text("SELECT id, iso_code, name_en, name_zh, region FROM countries WHERE iso_code = :code"),
                {"code": code}
            ).first()
            return dict(row._mapping) if row else None

        elif entity_type == "city":
            row = session.execute(
                text("SELECT id, code, name_en, name_zh, country_iso, latitude, longitude FROM cities WHERE code = :code"),
                {"code": code}
            ).first()
            return dict(row._mapping) if row else None

        elif entity_type == "company":
            row = session.execute(
                text("SELECT id, code, name_en, name_zh, sector FROM companies WHERE code = :code"),
                {"code": code}
            ).first()
            return dict(row._mapping) if row else None

        elif entity_type in SYMBOL_TYPE_MAP:
            symbol_types = SYMBOL_TYPE_MAP[entity_type]
            types_str = ", ".join([f"'{t}'" for t in symbol_types])
            row = session.execute(
                text(f"""SELECT id, ticker, symbol_type, name_en, name_zh, exchange, company_code
                        FROM symbols WHERE ticker = :code AND symbol_type IN ({types_str})"""),
                {"code": code}
            ).first()
            return dict(row._mapping) if row else None

        else:
            return None
    finally:
        session.close()


def count_by_type() -> dict[str, int]:
    """Return counts of entities by type. Useful for health checks."""
    engine = default_engine()
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        counts = {}

        # Countries
        row = session.execute(text("SELECT COUNT(*) FROM countries")).first()
        counts["country"] = row[0] if row else 0

        # Cities
        row = session.execute(text("SELECT COUNT(*) FROM cities")).first()
        counts["city"] = row[0] if row else 0

        # Companies
        row = session.execute(text("SELECT COUNT(*) FROM companies")).first()
        counts["company"] = row[0] if row else 0

        # Symbols by type
        for etype, types in SYMBOL_TYPE_MAP.items():
            types_str = ", ".join([f"'{t}'" for t in types])
            row = session.execute(
                text(f"SELECT COUNT(*) FROM symbols WHERE symbol_type IN ({types_str})")
            ).first()
            counts[etype] = row[0] if row else 0

        return counts
    finally:
        session.close()


if __name__ == "__main__":
    # Health check
    print("=== Entity Taxonomy Health Check ===")
    counts = count_by_type()
    for etype, count in counts.items():
        print(f"  {etype}: {count}")

    # Sample lookups
    print("\n=== Sample Lookups ===")
    cn = find_entity("country", "CN")
    if cn:
        print(f"  China: {cn}")

    apple = find_entity("stock", "AAPL")
    if apple:
        print(f"  AAPL: {apple}")

    sample = find_entity("company", "AAPL")
    if sample:
        print(f"  Company AAPL: {sample}")
