"""Relationship resolution helpers for entity lookups.

This module provides functions to resolve relationships between entities,
such as stock-to-industry mappings and company-to-sector mappings.
"""

import os
from typing import Optional, List, Dict
from sqlalchemy import create_engine, text

# Database connection configuration (canonical DB: guangzhou-xinru:30432, user fd,
# db fd_open_data — set PG_PASSWORD via env; never hardcode)
PG_HOST = os.environ.get("PG_HOST", "guangzhou-xinru")
PG_PORT = int(os.environ.get("PG_PORT", 30432))
PG_USER = os.environ.get("PG_USER", "fd")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
PG_DATABASE = os.environ.get("PG_DATABASE", "fd_open_data")

DATABASE_URL = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"


def get_engine():
    """Create SQLAlchemy engine for database connection."""
    return create_engine(DATABASE_URL)


def resolve_stock_industry(stock_code: str) -> List[Dict]:
    """Resolve industry classifications for a stock.

    Args:
        stock_code: Stock ticker code (e.g., "600000.SH")

    Returns:
        List of dicts with keys: industry_code, industry_name, level

    Note:
        This function requires a stock-industry mapping table which is not
        currently available in the database. Returns empty list until the
        data is populated.

    Examples:
        >>> resolve_stock_industry("600000.SH")
        [{'industry_code': 'shenwan_1_26', 'industry_name': '银行', 'level': 1}]
    """
    # TODO: Implement when stock-industry mapping table is available
    # The astock_daily table doesn't have industry_code column
    # Need to create a mapping table or use external data source
    print(f"Note: Stock-industry mapping not available for {stock_code}")
    return []


def resolve_company_sector(company_code: str) -> Optional[Dict]:
    """Resolve sector information for a company.

    Args:
        company_code: Company code (e.g., "AAPL")

    Returns:
        Dict with keys: sector, sector_name, or None if not found

    Examples:
        >>> resolve_company_sector("AAPL")
        {'sector': 'Technology', 'sector_name': 'Technology'}
    """
    engine = get_engine()
    query = """
        SELECT sector
        FROM companies
        WHERE code = :company_code
    """

    try:
        with engine.connect() as conn:
            result = conn.execute(text(query), {"company_code": company_code})
            row = result.fetchone()
            if row and row.sector:
                return {
                    "sector": row.sector,
                    "sector_name": row.sector
                }
            return None
    except Exception as e:
        print(f"Warning: Could not resolve sector for {company_code}: {e}")
        return None


def resolve_industry_stocks(industry_code: str) -> List[str]:
    """Resolve all stocks belonging to an industry.

    Args:
        industry_code: Industry code (e.g., "shenwan_1_26")

    Returns:
        List of stock codes

    Examples:
        >>> resolve_industry_stocks("shenwan_1_26")
        ['600000.SH', '601398.SH', ...]
    """
    engine = get_engine()
    query = """
        SELECT DISTINCT stock_id
        FROM astock_daily
        WHERE industry_code = :industry_code
        ORDER BY stock_id
    """

    try:
        with engine.connect() as conn:
            result = conn.execute(text(query), {"industry_code": industry_code})
            return [row.stock_id for row in result]
    except Exception as e:
        print(f"Warning: Could not resolve stocks for industry {industry_code}: {e}")
        return []


def check_stock_in_industry(stock_code: str, industry_code: str) -> bool:
    """Check if a stock belongs to a specific industry.

    Args:
        stock_code: Stock ticker code
        industry_code: Industry code

    Returns:
        True if stock belongs to industry, False otherwise

    Examples:
        >>> check_stock_in_industry("600000.SH", "shenwan_1_26")
        True
    """
    industries = resolve_stock_industry(stock_code)
    return any(ind["industry_code"] == industry_code for ind in industries)


def batch_resolve_stock_industries(stock_codes: List[str]) -> Dict[str, List[Dict]]:
    """Batch resolve industries for multiple stocks.

    Args:
        stock_codes: List of stock codes

    Returns:
        Dict mapping stock_code to list of industries

    Examples:
        >>> batch_resolve_stock_industries(["600000.SH", "601398.SH"])
        {
            '600000.SH': [{'industry_code': 'shenwan_1_26', ...}],
            '601398.SH': [{'industry_code': 'shenwan_1_26', ...}]
        }
    """
    result = {}
    for code in stock_codes:
        result[code] = resolve_stock_industry(code)
    return result
