## Why

The semantic layer cannot retrieve stock data for major A-shares such as 中国银行 (601988, entity 3112) and 平安银行 (000001, entity 3117). `read()` returns nothing, `fetch()` reports "no source succeeded", and `ai_search` steers callers toward concepts that have no bindings. Yet the layer is not empty: System-B stock concepts (`price.close`, `financials.revenue`, …) are fully wired (117 bindings, 18.5M observations for 6275 entities) and `semantic_observations` holds 96M rows. The failure is a wiring gap — wrong source identifiers, a parallel ghost concept system, discovery that surfaces unbound concepts, and missing ingestion — not a missing-data problem.

## What Changes

- **Fix stock source identifiers.** Stock entities currently resolve to garbage akshare/yfinance identifiers (中国银行 → `02211`, 平安银行 → `02219`) instead of the ticker (`601988`, `000001`). Re-seed `entity_source_identifiers` for all 5212 `stock` entities so akshare = 6-digit ticker and yfinance = exchange-suffixed (`601988.SS` / `000001.SZ`). This unblocks `fetch()` ranked dispatch.
- **Deprecate the ghost `symbol` concept system for stocks.** 120 `entity_type=symbol` concepts (`PRICE_CLOSE`, `PS_REVENUE`, `FIN_ROE`, …) have **zero** bindings and zero observations; they duplicate the 27 canonical `entity_type=stock` concepts (`price.close`, `financials.revenue`, …) which ARE wired and populated. Mark the `symbol`-typed stock duplicates as deprecated and exclude them from discovery. **BREAKING** for any caller referencing the `PRICE_*` / `PS_*` / `FIN_*` symbol-concept codes (none have bindings or data today).
- **Gate discovery on binding presence.** `ai_search`, `semantic_search`, and `semantic_search_unified` SHALL not surface concepts with zero bindings as primary results; bound concepts are ranked ahead of unbound ones (or unbound concepts are excluded entirely behind a flag).
- **Ingest missing stocks.** After the identifier fix, backfill `semantic_observations` for the 2934 stocks with zero observations (including 中国银行 and 平安银行) via ranked dispatch / a crawl run.
- **Harden `read`/`fetch` resilience.** `read()` crashed the MCP server ("Connection closed") under a multi-date stock query against the 96M-row table; add query scoping / pagination so large-table reads do not drop the connection.

## Capabilities

### New Capabilities
- `stock-source-identity`: Correct, canonical mapping of `stock` entities to per-source identifiers (akshare 6-digit ticker, yfinance exchange-suffixed, cn-report 6-digit). Covers seeding, validation, and the rule that the stock `code` is the akshare identifier.
- `concept-canonicalization`: One canonical concept per (category, metric). Duplicate/legacy concepts (e.g. the `symbol`-typed `PRICE_*`/`PS_*`/`FIN_*` set) are marked deprecated and excluded from discovery and dispatch; only the canonical (`stock`-typed) twin is served.

### Modified Capabilities
- `entity-semantic-search`: Discovery (`semantic_search`, `semantic_search_entities`, `semantic_search_unified`, `ai_search`) SHALL prefer concepts that have at least one binding, and SHALL not return unbound deprecated concepts as primary results.

## Impact

- **fd-open-data-mcp / `catalog/`**: re-seed `entity_source_identifiers` for stocks; add a validation/repair CLI subcommand; mark 120 `symbol` concepts deprecated (new `status`/`deprecated` flag on `concepts`, or a dedicated deprecation table).
- **fd-open-data-mcp / `semantic_search.py`, `ai_search.py`**: filter/rerank by binding presence; exclude deprecated concepts.
- **fd-open-data-mcp / `server.py` `read`/`fetch`**: scope multi-date queries (date-range LIMIT, chunking) so the 96M-row table does not kill the MCP connection; keep entity_type validation (it is correct — the bug was discovery leading callers to the wrong concept type).
- **Database**: additive migration (deprecation flag/column on `concepts`; corrected `entity_source_identifiers` rows). No schema removal.
- **Data ops**: a one-shot backfill/crawl run for the 2934 zero-observation stocks after identifiers are fixed.
- **Backward compatibility**: BREAKING for references to the deprecated `symbol` stock-concept codes; those concepts had no bindings or data, so no live consumer is affected. Non-stock `symbol` concepts (futures/crypto/ETF/index/technical-indicator) are unaffected and remain canonical for their domains.
