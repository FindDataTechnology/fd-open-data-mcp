# Entity Type Vocabulary: stock vs symbol

## Overview

The fd-open-data-mcp ontology uses two distinct entity types for financial instruments:
- **`stock`**: Individual stock price data and financial statements
- **`symbol`**: Broader market instruments (ETFs, funds, indices, crypto, futures, bonds)

## Distinction

### `stock` Entity Type
- **Scope**: Individual equity securities (A-shares, H-shares, US stocks)
- **Concepts**: 22 concepts covering:
  - Price data: `price.close`, `price.open`, `price.high`, `price.low`, `price.amount`, `price.volume`
  - Financial statements: `financials.revenue`, `financials.net_income`, `financials.total_assets`, etc.
- **Data source**: akshare stock history, financial reports
- **Example entities**: `600519` (贵州茅台), `000858` (五粮液), `AAPL` (Apple)

### `symbol` Entity Type
- **Scope**: All other tradable instruments beyond individual stocks
- **Concepts**: 120 concepts covering:
  - ETF indicators: `ETF_DISCOUNT`, `ETF_DIVIDEND`, `ETF_NAV`, `ETF_SIZE`
  - Profit statement (PS_*): `PS_BASIC_EPS`, `PS_NET_PROFIT`, `PS_REVENUE`
  - Balance sheet (BS_*): `BS_TOTAL_ASSETS`, `BS_TOTAL_LIABILITIES`
  - Cash flow (CF_*): `CF_OPERATING`, `CF_FREE_CASH_FLOW`
  - Technical analysis (TA_*): `TA_SMA_20`, `TA_MACD`, `TA_BOLL_MID`
  - Price data: `PRICE_CLOSE`, `PRICE_OPEN`, `PRICE_HIGH`, `PRICE_LOW`
  - Crypto: `CRYPTO_MARKET_CAP`, `CRYPTO_PRICE`, `CRYPTO_VOLUME`
  - Futures: `FUT_OPEN`, `FUT_OPEN_INTEREST`
  - Index: `IDX_PRICE`, `IDX_CHANGE_PCT`
- **Data source**: akshare market data, yfinance, crypto exchanges
- **Example entities**: ETFs, funds, indices, crypto tokens, futures contracts

## Why Two Types?

The distinction reflects different data granularity and use cases:

1. **Stock-specific analysis**: When analyzing individual company fundamentals (financial statements, price history), use `stock` entity type.

2. **Broader market analysis**: When analyzing ETFs, indices, crypto, or other instruments, use `symbol` entity type.

3. **Data source mapping**: Different data sources provide different levels of granularity. akshare's stock history API maps to `stock`, while its ETF/index APIs map to `symbol`.

## Identifier Resolution

Both entity types use the same identifier resolution mechanism:
- `stock`: ticker symbol (e.g., `600519`, `AAPL`)
- `symbol`: instrument code (e.g., `510300` for ETF, `BTC-USD` for crypto)

The `entity_source_identifiers` table stores per-source identifiers for both types:
```sql
INSERT INTO entity_source_identifiers (entity_type, entity_id, source, identifier)
VALUES ('stock', 1, 'akshare', '600519'),
       ('stock', 1, 'yfinance', '600519.SS'),
       ('symbol', 2, 'akshare', '510300');
```

## Migration Path

If you need to unify these types in the future:
1. Create a new `security` entity type that encompasses both
2. Migrate all `stock` and `symbol` concepts to `security`
3. Update identifier resolution to handle both ticker symbols and instrument codes
4. Update data source adapters to map to the unified type

For now, keep them separate to preserve the semantic distinction between individual stocks and broader market instruments.
