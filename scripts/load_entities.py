#!/usr/bin/env python3
"""Load entity definitions from PostgreSQL database for manifest generation.

This module provides functions to load entity definitions from the remote
PostgreSQL database (192.168.1.4:5433) for use in datasource manifests.
"""

import os
from typing import Optional

from sqlalchemy import create_engine, text

# Database connection configuration
PG_HOST = os.environ.get("PG_HOST", "192.168.1.4")
PG_PORT = int(os.environ.get("PG_PORT", 5433))
PG_USER = os.environ.get("PG_USER", "admin")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "admin123")
PG_DATABASE = os.environ.get("PG_DATABASE", "postgres")

DATABASE_URL = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"


def get_engine():
    """Create SQLAlchemy engine for database connection."""
    return create_engine(DATABASE_URL)


def load_country_entities(limit: Optional[int] = None) -> list[dict]:
    """Load all countries from the countries table.

    Returns list of dicts compatible with Entity model:
    {
        "entity_type": "country",
        "code": "CN",
        "name_en": "China",
        "name_zh": "中国",
        "metadata": {"region": "Asia"}
    }
    """
    engine = get_engine()
    query = """
        SELECT iso_code, name_en, name_zh, region
        FROM countries
        ORDER BY iso_code
    """
    if limit:
        query += f" LIMIT {limit}"

    with engine.connect() as conn:
        result = conn.execute(text(query))
        return [
            {
                "entity_type": "country",
                "code": row.iso_code,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "metadata": {"region": row.region}
            }
            for row in result
        ]


def load_industry_entities(level: Optional[int] = None, limit: Optional[int] = None) -> list[dict]:
    """Load industry classifications from the astock_industry_board table.

    Args:
        level: Not used (astock_industry_board has no level column)
        limit: Maximum number of industries to load.

    Returns list of dicts compatible with Entity model.
    """
    engine = get_engine()
    # Get unique industry boards from the performance table
    query = """
        SELECT DISTINCT board_code, board_name
        FROM astock_industry_board
        WHERE board_name IS NOT NULL
        ORDER BY board_code
    """
    if limit:
        query += f" LIMIT {limit}"

    with engine.connect() as conn:
        result = conn.execute(text(query))
        entities = []
        for row in result:
            entity = {
                "entity_type": "industry",
                "code": row.board_code.strip(),
                "name_en": row.board_name.strip() if row.board_name else None,
                "name_zh": row.board_name.strip() if row.board_name else None,
                "metadata": {
                    "classification_system": "shenwan",
                    "level": 1,  # Default to level 1
                }
            }
            entities.append(entity)
        return entities


def load_company_entities(limit: Optional[int] = None) -> list[dict]:
    """Load companies from the companies table.

    Returns list of dicts compatible with Entity model.
    """
    engine = get_engine()
    query = """
        SELECT code, name_en, name_zh, sector
        FROM companies
        ORDER BY code
    """
    if limit:
        query += f" LIMIT {limit}"

    with engine.connect() as conn:
        result = conn.execute(text(query))
        return [
            {
                "entity_type": "company",
                "code": row.code,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "metadata": {"sector": row.sector} if row.sector else {}
            }
            for row in result
        ]


def load_stock_entities(limit: Optional[int] = None) -> list[dict]:
    """Load stocks from the symbols table.

    Returns list of dicts compatible with Entity model.
    """
    engine = get_engine()
    query = """
        SELECT ticker, name_en, name_zh, exchange, company_code
        FROM symbols
        WHERE symbol_type = 'stock'
        ORDER BY ticker
    """
    if limit:
        query += f" LIMIT {limit}"

    with engine.connect() as conn:
        result = conn.execute(text(query))
        return [
            {
                "entity_type": "stock",
                "code": row.ticker,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "metadata": {
                    "exchange": row.exchange,
                    "company_code": row.company_code
                } if row.exchange or row.company_code else {}
            }
            for row in result
        ]


def load_fund_entities(limit: Optional[int] = None) -> list[dict]:
    """Load funds from the symbols table.

    Returns list of dicts compatible with Entity model.
    """
    engine = get_engine()
    query = """
        SELECT ticker, name_en, name_zh, exchange
        FROM symbols
        WHERE symbol_type IN ('fund', 'etf')
        ORDER BY ticker
    """
    if limit:
        query += f" LIMIT {limit}"

    with engine.connect() as conn:
        result = conn.execute(text(query))
        return [
            {
                "entity_type": "fund",
                "code": row.ticker,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "metadata": {"exchange": row.exchange} if row.exchange else {}
            }
            for row in result
        ]


def count_entities_by_type() -> dict[str, int]:
    """Count entities by type for health checks.

    Returns dict mapping entity_type to count.
    """
    engine = get_engine()
    counts = {}

    with engine.connect() as conn:
        # Countries
        result = conn.execute(text("SELECT COUNT(*) FROM countries"))
        counts["country"] = result.scalar()

        # Industries (unique boards from astock_industry_board)
        result = conn.execute(text("SELECT COUNT(DISTINCT board_code) FROM astock_industry_board"))
        counts["industry"] = result.scalar()

        # Companies
        result = conn.execute(text("SELECT COUNT(*) FROM companies"))
        counts["company"] = result.scalar()

        # Stocks
        result = conn.execute(text("SELECT COUNT(*) FROM symbols WHERE symbol_type = 'stock'"))
        counts["stock"] = result.scalar()

        # Funds
        result = conn.execute(text("SELECT COUNT(*) FROM symbols WHERE symbol_type IN ('fund', 'etf')"))
        counts["fund"] = result.scalar()

    return counts


if __name__ == "__main__":
    # Health check
    print("=== Entity Loader Health Check ===")
    counts = count_entities_by_type()
    for etype, count in counts.items():
        print(f"  {etype}: {count}")

    # Sample loads
    print("\n=== Sample Country Entities ===")
    countries = load_country_entities(limit=5)
    for c in countries:
        print(f"  {c['code']}: {c['name_en']} ({c['name_zh']})")

    print("\n=== Sample Industry Entities ===")
    industries = load_industry_entities(limit=5)
    for i in industries:
        print(f"  {i['code']}: {i['name_en']} (Level {i['metadata']['level']})")
