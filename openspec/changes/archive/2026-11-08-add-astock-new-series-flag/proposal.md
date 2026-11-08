# Let an explicitly named A-share code create its own series

## Why

The astock family covers **4,576** bare A-share codes, but akshare's identifier
table holds **6,401** six-digit codes — **1,825 codes have no `astock_daily`
series at all** (mostly recent listings: 001206, 001217, 001231, 001257 …,
measured 2026-11-08 on the master).

They can never appear on their own: `_astock_ident()` filters every candidate
by "does this entity already hold rows" (`_ids_with_rows`), and `--universe`
derives the scope from that same filtered set. So the standing pipeline can only
keep the existing family moving — the family cannot grow. Worse, naming such a
code explicitly (`--symbols 001206`) also writes **nothing**, silently: it lands
in the `missing` list and the run reports a healthy `done: 0 rows`.

That filter is not gratuitous: when a code has both a bare and a suffixed entity
(`600000` and `600000.SH`), row presence is what picks the entity that really
belongs to the family. Dropping it unconditionally would let a suffixed twin
become a second, split series for the same stock.

## What changes

- New opt-in flag `--allow-new-series` on `bulk_ingest_astock_daily.py`. With
  it, a code named explicitly (`--symbols` / `--symbols-file`) whose candidate
  entities hold **no** rows is kept instead of dropped: one entity per bare code,
  the bare identifier preferred over a suffixed twin.
- Presence still wins: if any candidate for that code holds rows, that entity is
  used and the flag changes nothing.
- `--universe` keeps the existing presence filter under all circumstances, so
  the family cannot grow as a side effect of a routine run.
- The run log names the codes that will create a new series, and `--dry-run`
  reports the same set without writing.

## Impact

- Affected specs: new capability `astock-daily-series`.
- Affected code: `scripts/bulk_ingest_astock_daily.py` (`_astock_ident`,
  `resolve_entities`, CLI + logging) and its ConfigMap copy in `fd-scripts`;
  `tests/test_astock_gap_probe.py` gains cases for the new flag.
- Runtime: no package release needed — the reconciler reads the script from the
  `fd-scripts` ConfigMap at launch, so the flag ships by updating that key.
- Not in scope: which of the 1,825 codes *should* be in the family (that is a
  data-scope decision); the flag only makes the operation possible.
