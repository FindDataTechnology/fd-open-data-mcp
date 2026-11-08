# A code→entity map must join the entity, not trust the identifier's own type

## Why

Both financial-statement ingesters build their code→entity map from the
identifier table alone:

```python
rows = conn.execute(text(
    "SELECT identifier, entity_id FROM entity_source_identifiers "
    "WHERE entity_type=:et AND source=:src"), {"et": "stock", "src": "akshare"})
out[code] = r[1]          # later rows WIN
```

`entity_source_identifiers.entity_type` is a *claim* that the 2026-11-08
correction proved can be wrong. When a stock-typed identifier hangs off a
non-stock entity, this map silently points a real ticker at the wrong entity:
entity 2053 is the **industry** 医药生物 (code 801150), yet it carried
`00712`/`00712.HK` typed `stock`, so `000712` mapped to 2053 and the ingester
wrote 锦龙股份's statements onto 医药生物.

Measured 2026-11-08: **8,500 observations** of `financials.*` (24 concepts,
2015-03-31 … 2026-09-30, source `akshare`) sit on 11 **industry** entity ids,
written as recently as 2026-09-30 — a live writer, not history.

## What changes

- The code→entity map joins `entities` and requires `entity_type='stock'` **on
  the entity**, so a mislabelled identifier can no longer redirect a company's
  financials.
- Collisions are reported instead of silently overwritten: if two stock entities
  claim one code, the map keeps the first and logs the conflict.
- A guard test pins the property: a stock-typed identifier on an industry entity
  must not enter the map.

## Impact

- Affected specs: `entity-identifier-integrity`.
- Affected code: `scripts/bulk_ingest_financials_aggregate.py`,
  `scripts/bulk_ingest_stock_financials.py`, plus tests. These are
  ConfigMap/script-delivered, so no image release is needed.
- Existing mis-homed rows are **not** re-pointed here, and they must **not** be:
  the codes are **Hong Kong tickers** (``00712``, ``01377``, ``01586`` — five
  digits, with ``.HK`` siblings), so zero-filling them to six digits lands on an
  unrelated A-share company (``00712`` -> ``000712`` 锦龙股份, ``02001`` ->
  ``002001`` 新和成 …). A first pass counted 16 "targets" with zero key
  collisions precisely because the collision is *between companies*, not between
  rows — re-pointing would have attributed one company's statements to another.
  The real prerequisite is creating HK stock entities for those five-digit codes
  (tracked separately); until then the rows stay and the guard keeps the class
  in its documented allowlist.
