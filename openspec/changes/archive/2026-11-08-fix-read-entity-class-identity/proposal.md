# A series read must resolve the entity's own class, not the row's declared one

## Why

0.5.50 tried to close the mis-attached-data hole by pinning
`semantic_observations.entity_type` to `concepts.entity_type` — the class the
**row declares**. The mis-attached rows declare `'stock'` themselves, exactly
like a genuine stock row, so the predicate could not see them: a caller passing
an industry or fund-manager entity id to a stock concept still read back another
class's price/financials.

Measured against production on 2026-11-08 (`semantic_observations`, 7,159,529
rows), rows whose declared class is not the class of the entity they hang off:

| row declares | entity is | rows |
|---|---|---|
| stock | **person** | **333,975** |
| stock | industry | 8,500 |
| country | city | 6,302 |
| fund | stock | 2,027 |
| stock | fund | 936 |
| region | country | 96 |
| company | stock | 12 |

351,740 rows total; 11 further `entity_id`s are dangling. The largest group was
previously unknown: 865 fund-manager `person` entities (identifiers are
eastmoney manager ids such as `1298`) each carry ~443 OHLCV rows
(`price.high/low/open/close/amount`, 2026-06-09 … 2026-09-18, source `akshare`).

The same 0.5.50 predicate also *hid real data*: the star-rated survey stores one
region-typed concept per city, province and country, so its 5,565 city rows hang
off `city` entities and were unreadable at city level.

## What changes

- A read resolves the entity: the row's declared class SHALL equal the class of
  the entity it hangs off. This is the only predicate that separates the
  mis-attached rows from genuine ones, and it keeps the multi-level series.
- For security concepts (`stock`) the entity SHALL additionally be a stock
  entity; cross-class attachment has no legitimate form there.
- The check is a loud error at the cache read entries (which `read`, `fetch`,
  `read_series` and gap-fill all pass through) and a SQL predicate in
  `hotel_series` / the hotel KPI branch / the analysis backend.

## What does not change

The 351,740 rows themselves. Whether they are deleted or migrated to the correct
entity is a separate, destructive decision; after this change they can neither be
written nor read, so leaving them in place no longer pollutes any answer.
