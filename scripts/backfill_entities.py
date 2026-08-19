"""Backfill entities table from existing data sources.

Phase 2 of add-entity-graph-vector-search change. Populates the unified entities
table from existing data in the remote Postgres database (countries, cities,
companies, symbols).

Usage: python scripts/backfill_entities.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlalchemy import text
from fd_open_data_mcp import db as dbmod

# Force remote Postgres connection
DATABASE_URL = "postgresql://fd:FD_PG_PASSWORD@guangzhou-xinru:30432/fd_open_data"
os.environ["FD_OPEN_DATA_MCP_DATABASE_URL"] = DATABASE_URL


def backfill_countries(session):
    """Backfill country entities from countries table."""
    print("Backfilling countries...")

    # Get all countries
    result = session.execute(text("""
        SELECT id, iso_code, name_en, name_zh, region
        FROM countries
    """))

    count = 0
    for row in result:
        # Check if already exists
        existing = session.execute(
            text("SELECT id FROM entities WHERE entity_type = 'country' AND code = :code"),
            {"code": row.iso_code}
        ).first()

        if existing:
            continue

        # Insert new entity
        metadata = {
            "region": row.region,
            "original_id": row.id,
        }

        session.execute(
            text("""
                INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json)
                VALUES ('country', :code, :name_en, :name_zh, :metadata)
            """),
            {
                "code": row.iso_code,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "metadata": json.dumps(metadata),
            }
        )
        count += 1

    print(f"  Inserted {count} countries")
    return count


def backfill_cities(session):
    """Backfill city entities from cities table."""
    print("Backfilling cities...")

    # Get all cities
    result = session.execute(text("""
        SELECT id, code, name_en, name_zh, country_iso, latitude, longitude
        FROM cities
    """))

    count = 0
    for row in result:
        # Check if already exists
        existing = session.execute(
            text("SELECT id FROM entities WHERE entity_type = 'city' AND code = :code"),
            {"code": row.code}
        ).first()

        if existing:
            continue

        # Insert new entity
        metadata = {
            "country_iso": row.country_iso,
            "latitude": row.latitude,
            "longitude": row.longitude,
            "original_id": row.id,
        }

        session.execute(
            text("""
                INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json)
                VALUES ('city', :code, :name_en, :name_zh, :metadata)
            """),
            {
                "code": row.code,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "metadata": json.dumps(metadata),
            }
        )
        count += 1

    print(f"  Inserted {count} cities")
    return count


def backfill_companies(session):
    """Backfill company entities from companies table."""
    print("Backfilling companies...")

    # Get all companies
    result = session.execute(text("""
        SELECT id, code, name_en, name_zh, sector
        FROM companies
    """))

    count = 0
    for row in result:
        # Check if already exists
        existing = session.execute(
            text("SELECT id FROM entities WHERE entity_type = 'company' AND code = :code"),
            {"code": row.code}
        ).first()

        if existing:
            continue

        # Insert new entity
        metadata = {
            "sector": row.sector,
            "original_id": row.id,
        }

        session.execute(
            text("""
                INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json)
                VALUES ('company', :code, :name_en, :name_zh, :metadata)
            """),
            {
                "code": row.code,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "metadata": json.dumps(metadata),
            }
        )
        count += 1

    print(f"  Inserted {count} companies")
    return count


def backfill_symbols(session):
    """Backfill symbol entities (stocks, ETFs, etc.) from symbols table."""
    print("Backfilling symbols...")

    # Get all symbols
    result = session.execute(text("""
        SELECT id, ticker, symbol_type, name_en, name_zh, exchange, company_code
        FROM symbols
    """))

    count = 0
    for row in result:
        # Map symbol_type to entity_type
        entity_type_map = {
            "stock": "stock",
            "etf": "etf",
            "bond": "bond",
            "coin": "crypto",
            "index": "index",
            "fund": "fund",
            "future": "future",
        }

        entity_type = entity_type_map.get(row.symbol_type, "stock")

        # Check if already exists
        existing = session.execute(
            text("SELECT id FROM entities WHERE entity_type = :entity_type AND code = :code"),
            {"entity_type": entity_type, "code": row.ticker}
        ).first()

        if existing:
            continue

        # Insert new entity
        metadata = {
            "symbol_type": row.symbol_type,
            "exchange": row.exchange,
            "company_code": row.company_code,
            "original_id": row.id,
        }

        session.execute(
            text("""
                INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json)
                VALUES (:entity_type, :code, :name_en, :name_zh, :metadata)
            """),
            {
                "entity_type": entity_type,
                "code": row.ticker,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "metadata": json.dumps(metadata),
            }
        )
        count += 1

    print(f"  Inserted {count} symbols")
    return count


def main():
    """Run the entity backfill."""
    print("=== Entity Backfill ===\n")

    # Get database session
    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Backfill from each source
        total = 0
        total += backfill_countries(session)
        total += backfill_cities(session)
        total += backfill_companies(session)
        total += backfill_symbols(session)

        session.commit()

        print(f"\n=== Backfill Complete ===")
        print(f"Total entities inserted: {total}")

        # Verify counts
        result = session.execute(text("""
            SELECT entity_type, COUNT(*) as count
            FROM entities
            GROUP BY entity_type
            ORDER BY count DESC
        """))

        print("\nEntity counts by type:")
        for row in result:
            print(f"  {row.entity_type}: {row.count}")

    except Exception as e:
        session.rollback()
        print(f"\nError during backfill: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
