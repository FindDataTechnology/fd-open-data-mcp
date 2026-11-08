# Identifiers must carry their entity's type

## Why

`entity_source_identifiers.entity_type` disagrees with the owning row in
`entities` for **2,971 identifiers** (measured 2026-11-08 on the master):

| identifier type | entity type | rows | entities |
|---|---|---|---|
| stock | **person** | 1,793 | 895 |
| stock | fund | 843 | 283 |
| stock | organization | 240 | 80 |
| country | city | 36 | 18 |
| stock | industry / index / exchange | 59 | 25 |

The largest class is the dangerous one: `akshare`'s `fund_manager_em` ingestion
wrote each manager's **managed fund codes** as `entity_type='stock'` identifiers
onto the **person** entity (e.g. 纪骏炜 ← `510810`/`510810.SS`, 金泽宇 ←
`512660`). Any code→entity resolution that trusts the identifier table can
therefore resolve an ETF code to a *person's* entity id.

This also explains most of the earlier "1,825 A-share codes with no series":
they were fund codes hanging off person entities, not missing stocks — the true
A-share gap was 626 (already backfilled).

## What changes

- Delete the `stock`-typed identifiers whose entity is a **person** (a person
  does not have a stock code; these are ingestion artifacts). Snapshot first.
- Re-type the `stock`-typed identifiers whose entity is a **fund** to `fund`
  (the entity is right, the label was wrong).
- Leave the small organization/industry/index/exchange and country-on-city
  classes untouched and recorded as debt: each needs a per-class decision.
- Verification: re-run the mismatch matrix (only the deliberately-deferred
  classes may remain), confirm the astock family is unchanged (it resolves by
  six-digit prefix, so it never read these rows), and confirm no observation
  referenced the deleted identifiers' entity ids as stock.

## Impact

- Affected specs: new capability `entity-identifier-integrity`.
- Affected data: `entity_source_identifiers` (2,636 rows: 1,793 deleted, 843
  re-typed); snapshot file kept locally, not in the repo (it carries codes, not
  credentials).
- Affected code: none — this is a data correction. A follow-up should stop the
  fund-manager ingestion from writing stock-typed identifiers at all.
- Not in scope: the fund-manager ingestion fix itself (different repo/pipeline).
