"""Backfill concept metadata (name_en, name_zh, category) for stock/industry concepts.

This script populates missing descriptions in the `concepts` table for:
- `stock` entity type: price.*, financials.* concepts
- `industry` entity type: currently placeholder doc.* concepts

Usage: python scripts/backfill_concept_metadata.py
"""
from __future__ import annotations

import os
from sqlalchemy import create_engine, text

# Connection to the remote ontology DB
PG_HOST = os.environ.get("PG_HOST", "192.168.1.4")
PG_PORT = int(os.environ.get("PG_PORT", 5433))
PG_USER = os.environ.get("PG_USER", "admin")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "admin123")
PG_DATABASE = os.environ.get("PG_DATABASE", "postgres")

DATABASE_URL = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"


# Stock concept descriptions
STOCK_DESCRIPTIONS = {
    # Price data
    "price.close": {
        "name_en": "Closing Price",
        "name_zh": "收盘价",
        "category": "Price Data",
        "unit": "currency",
        "frequency": "daily",
    },
    "price.open": {
        "name_en": "Opening Price",
        "name_zh": "开盘价",
        "category": "Price Data",
        "unit": "currency",
        "frequency": "daily",
    },
    "price.high": {
        "name_en": "Highest Price",
        "name_zh": "最高价",
        "category": "Price Data",
        "unit": "currency",
        "frequency": "daily",
    },
    "price.low": {
        "name_en": "Lowest Price",
        "name_zh": "最低价",
        "category": "Price Data",
        "unit": "currency",
        "frequency": "daily",
    },
    "price.amount": {
        "name_en": "Trading Amount",
        "name_zh": "成交额",
        "category": "Trading Data",
        "unit": "currency",
        "frequency": "daily",
    },
    "price.volume": {
        "name_en": "Trading Volume",
        "name_zh": "成交量",
        "category": "Trading Data",
        "unit": "shares",
        "frequency": "daily",
    },

    # Financial statements - Income Statement (利润表)
    "financials.revenue": {
        "name_en": "Total Revenue",
        "name_zh": "营业收入",
        "category": "Income Statement",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.net_income": {
        "name_en": "Net Income",
        "name_zh": "净利润",
        "category": "Income Statement",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.operating_profit": {
        "name_en": "Operating Profit",
        "name_zh": "营业利润",
        "category": "Income Statement",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.total_profit": {
        "name_en": "Total Profit",
        "name_zh": "利润总额",
        "category": "Income Statement",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.eps": {
        "name_en": "Earnings Per Share",
        "name_zh": "每股收益",
        "category": "Financial Ratios",
        "unit": "currency",
        "frequency": "yearly",
    },

    # Financial statements - Balance Sheet (资产负债表)
    "financials.total_assets": {
        "name_en": "Total Assets",
        "name_zh": "总资产",
        "category": "Balance Sheet",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.total_liabilities": {
        "name_en": "Total Liabilities",
        "name_zh": "总负债",
        "category": "Balance Sheet",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.equity": {
        "name_en": "Total Equity",
        "name_zh": "所有者权益合计",
        "category": "Balance Sheet",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.accounts_receivable": {
        "name_en": "Accounts Receivable",
        "name_zh": "应收账款",
        "category": "Balance Sheet",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.accounts_payable": {
        "name_en": "Accounts Payable",
        "name_zh": "应付账款",
        "category": "Balance Sheet",
        "unit": "currency",
        "frequency": "yearly",
    },

    # Financial statements - Cash Flow (现金流量表)
    "financials.operating_cash_flow": {
        "name_en": "Operating Cash Flow",
        "name_zh": "经营活动产生的现金流量净额",
        "category": "Cash Flow Statement",
        "unit": "currency",
        "frequency": "yearly",
    },

    # Ratios and metrics
    "financials.debt_ratio": {
        "name_en": "Debt Ratio",
        "name_zh": "资产负债率",
        "category": "Financial Ratios",
        "unit": "%",
        "frequency": "yearly",
    },
    "financials.retained_earnings": {
        "name_en": "Retained Earnings",
        "name_zh": "未分配利润",
        "category": "Balance Sheet",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.investment_income": {
        "name_en": "Investment Income",
        "name_zh": "投资收益",
        "category": "Income Statement",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.fixed_assets": {
        "name_en": "Fixed Assets",
        "name_zh": "固定资产",
        "category": "Balance Sheet",
        "unit": "currency",
        "frequency": "yearly",
    },
    "financials.share_capital": {
        "name_en": "Share Capital",
        "name_zh": "股本",
        "category": "Balance Sheet",
        "unit": "currency",
        "frequency": "yearly",
    },
}

# Industry concept descriptions (placeholder - needs real 申万 industry data)
INDUSTRY_DESCRIPTIONS = {
    "doc.date": {
        "name_en": "Report Date",
        "name_zh": "报告日期",
        "category": "Document Metadata",
    },
    "doc.title": {
        "name_en": "Report Title",
        "name_zh": "报告标题",
        "category": "Document Metadata",
    },
    "doc.url": {
        "name_en": "Document URL",
        "name_zh": "文档链接",
        "category": "Document Metadata",
    },
}


def main():
    """Backfill concept metadata."""
    engine = create_engine(DATABASE_URL)

    with engine.connect() as conn:
        updated = 0

        # Backfill stock concepts
        for code, desc in STOCK_DESCRIPTIONS.items():
            print(f"Processing stock concept: {code}")

            # Check if already has data
            result = conn.execute(
                text("SELECT COUNT(*) FROM concepts WHERE code = :code AND entity_type = 'stock'"),
                {"code": code}
            )
            count = result.scalar()
            if count == 0:
                print(f"  Skipping - no such concept")
                continue

            # Update only if empty columns exist
            update_sql = text("""
                UPDATE concepts
                SET name_en = COALESCE(name_en, :name_en),
                    name_zh = COALESCE(name_zh, :name_zh),
                    category = COALESCE(category, :category),
                    unit = COALESCE(unit, :unit),
                    frequency = COALESCE(frequency, :frequency)
                WHERE code = :code AND entity_type = 'stock'
            """)
            result = conn.execute(
                update_sql,
                {
                    "code": code,
                    "name_en": desc.get("name_en"),
                    "name_zh": desc.get("name_zh"),
                    "category": desc.get("category"),
                    "unit": desc.get("unit"),
                    "frequency": desc.get("frequency"),
                }
            )
            if result.rowcount > 0:
                updated += 1
                print(f"  Updated {result.rowcount} rows")

        # Backfill industry concepts
        for code, desc in INDUSTRY_DESCRIPTIONS.items():
            print(f"Processing industry concept: {code}")

            update_sql = text("""
                UPDATE concepts
                SET name_en = COALESCE(name_en, :name_en),
                    name_zh = COALESCE(name_zh, :name_zh),
                    category = COALESCE(category, :category)
                WHERE code = :code AND entity_type = 'industry'
            """)
            result = conn.execute(
                update_sql,
                {
                    "code": code,
                    "name_en": desc.get("name_en"),
                    "name_zh": desc.get("name_zh"),
                    "category": desc.get("category"),
                }
            )
            if result.rowcount > 0:
                updated += 1
                print(f"  Updated {result.rowcount} rows")

        conn.commit()
        print(f"\n=== Done ===")
        print(f"Updated {updated} concepts")

        # Verify results
        print("\n=== Verification ===")
        result = conn.execute(text("""
            SELECT entity_type,
                   COUNT(*) as total,
                   SUM(CASE WHEN name_en IS NOT NULL THEN 1 ELSE 0 END) as has_name_en,
                   SUM(CASE WHEN name_zh IS NOT NULL THEN 1 ELSE 0 END) as has_name_zh,
                   SUM(CASE WHEN category IS NOT NULL THEN 1 ELSE 0 END) as has_category
            FROM concepts
            WHERE entity_type IN ('stock', 'industry')
            GROUP BY entity_type
        """))
        for row in result:
            print(f"  {row.entity_type}: {row.total} total, "
                  f"{row.has_name_en} with name_en, "
                  f"{row.has_name_zh} with name_zh, "
                  f"{row.has_category} with category")


if __name__ == "__main__":
    main()
