"""Migrate legacy crawled data into the canonical ``semantic_observations`` store.

Reshapes wide/per-source legacy tables (on the same remote Postgres) into long,
concept-keyed observations - per the ``unified-data-store`` spec (D10). Does NOT
re-crawl: reads the legacy table and INSERT...SELECTs into ``semantic_observations``,
idempotent (ON CONFLICT DO NOTHING), joining ``symbol`` -> ``entity_id`` via
``entity_source_identifiers``.

Each migrator maps a legacy (table, value-column) -> a concept code; the concept_id
is resolved from the ``concepts`` table at runtime. Unmappable rows (no identifier,
no concept, NULL value) are skipped and reported.

Covers: ``astock_daily``/``astock_hk_daily``/``astock_us_daily`` (OHLCV), and the
financial statements (``astock_balance_sheet``/``profit_sheet``/``cash_flow``).
``astock_financial_indicators`` and record tables (research_report, block_trade, ...)
are NOT observations and are left in place.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

# OHLCV value-column -> concept code (all *_daily tables share this schema).
DAILY_COLUMNS: dict[str, str] = {
    "open": "price.open",
    "close": "price.close",
    "high": "price.high",
    "low": "price.low",
    "volume": "price.volume",
    "amount": "price.amount",
}

# Financial-statement (table, value-column) -> concept code. Date column = report_date.
FINANCIALS_MAP: dict[tuple[str, str], str] = {
    ("astock_balance_sheet", "total_assets"): "financials.total_assets",
    ("astock_balance_sheet", "total_liabilities"): "financials.total_liabilities",
    ("astock_balance_sheet", "total_equity"): "financials.equity",
    ("astock_profit_sheet", "revenue"): "financials.revenue",
    ("astock_profit_sheet", "net_profit"): "financials.net_income",
    ("astock_cash_flow", "operating_cf"): "financials.operating_cash_flow",
}


def _concept_lookup(session: Session, code: str, entity_type: str = "stock") -> tuple[Optional[int], Optional[str]]:
    row = session.execute(
        text("SELECT id, unit FROM concepts WHERE code=:code AND entity_type=:et LIMIT 1"),
        {"code": code, "et": entity_type},
    ).fetchone()
    return (int(row[0]), row[1]) if row else (None, None)


def _migrate_wide(
    session: Session,
    table: str,
    value_columns: dict[str, str],
    date_col: str,
    symbol_col: str = "symbol",
    entity_type: str = "stock",
    source: str = "akshare",
    symbol: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """Generic wide->long reshape: for each (value-column -> concept code), INSERT...SELECT
    from ``table`` joined to ``entity_source_identifiers`` on ``symbol_col``."""
    summary: dict = {"source_table": table, "symbol": symbol, "limit": limit, "columns": {}}
    for col, code in value_columns.items():
        concept_id, unit = _concept_lookup(session, code, entity_type)
        if concept_id is None:
            summary["columns"][col] = {"concept": code, "inserted": 0, "skipped_reason": "concept not found"}
            continue
        where = [f"d.{col} IS NOT NULL"]
        params: dict = {"concept_id": concept_id, "unit": unit or ""}
        if symbol is not None:
            where.append(f"d.{symbol_col} = :symbol")
            params["symbol"] = symbol
        limit_sql = " LIMIT :limit" if limit is not None else ""
        if limit is not None:
            params["limit"] = limit
        sql = text(f"""
            INSERT INTO semantic_observations
                (concept_id, entity_type, entity_id, date, value, unit, source_used, fetched_at)
            SELECT :concept_id, :et, esi.entity_id, d.{date_col}::text,
                   d.{col}::text, :unit, :source, now()
            FROM {table} d
            JOIN entity_source_identifiers esi
              ON esi.source = :source AND esi.entity_type = :et
             AND esi.identifier = d.{symbol_col}
            WHERE {' AND '.join(where)}{limit_sql}
            ON CONFLICT (concept_id, entity_type, entity_id, date) DO NOTHING
        """)
        params["et"] = entity_type
        params["source"] = source
        result = session.execute(sql, params)
        session.commit()  # commit per-column so progress is visible + resumable
        summary["columns"][col] = {"concept": code, "concept_id": concept_id, "inserted": result.rowcount}
    return summary


def migrate_astock_daily(session: Session, symbol: Optional[str] = None, limit: Optional[int] = None) -> dict:
    """Reshape ``astock_daily`` OHLCV into ``semantic_observations``."""
    return _migrate_wide(session, "astock_daily", DAILY_COLUMNS, date_col="trade_date",
                         symbol=symbol, limit=limit)


def migrate_stock_daily(session: Session, table: str, symbol: Optional[str] = None, limit: Optional[int] = None) -> dict:
    """Reshape ``astock_hk_daily`` or ``astock_us_daily`` OHLCV into ``semantic_observations``."""
    return _migrate_wide(session, table, DAILY_COLUMNS, date_col="trade_date",
                         symbol=symbol, limit=limit)


def migrate_financials(session: Session, table: str, symbol: Optional[str] = None, limit: Optional[int] = None) -> dict:
    """Reshape a financial-statement table (balance_sheet/profit_sheet/cash_flow) into
    ``semantic_observations``. Date = report_date; only columns with a concept map migrate."""
    cols = {c: code for (t, c), code in FINANCIALS_MAP.items() if t == table}
    if not cols:
        return {"source_table": table, "columns": {}, "skipped_reason": "no concept map for this table"}
    return _migrate_wide(session, table, cols, date_col="report_date",
                         symbol=symbol, limit=limit)
