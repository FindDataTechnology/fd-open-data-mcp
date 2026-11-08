# Fill each symbol's own gap, not one global window

## Why

The daily increment computes **one** window for the whole family: `start = (max
date over a small basket) + 1`. Any symbol that stops receiving bars — a
suspension, a failed fetch, a shard flap — therefore falls behind permanently:
the pipeline keeps asking for the last day or two while that symbol's own gap
grows, and nothing in the standing run ever goes back for it. Recovering it
needs a hand-built backfill (the 2026-11-08 repair wrote 1,773,415 rows exactly
because 3,836 symbols had drifted).

The pipeline already reads per-symbol anchors for qfq calibration
(`stored_anchors`), so the information needed to compute a per-symbol start is in
hand; only the window is global.

## What changes

- Each symbol's fetch window starts at **its own last stored date + 1**, floored
  at the run's global start (so a normal, current symbol is unchanged) and
  capped to the last `--max-catchup-days` (default **30**) so one badly lagging
  symbol cannot turn a daily run into a multi-hour backfill.
- A run logs how many symbols were caught up vs. bounded by the cap, so drift is
  visible instead of silent.
- `--start` still pins an explicit window for deliberate backfills (the flag
  wins over the per-symbol start).
- `existing_keys` idempotency is unchanged: already-stored (concept, entity,
  date) keys are still skipped, so a wide window costs fetches but never
  duplicates.

## Impact

- Affected specs: `astock-daily-series`.
- Affected code: `scripts/bulk_ingest_astock_daily.py` (`per_symbol_start()`
  helper, `ingest_symbol` gains a start argument, main loop wires the cap) and
  its `fd-scripts` ConfigMap copy; `tests/test_astock_gap_probe.py` gains cases.
- Runtime: ConfigMap-only change — the reconciler reads the script at launch, so
  no image release is needed.
- Load: bounded by the cap; a lagging symbol catches up 30 days per run, and the
  cap is configurable per policy.
- Not in scope: threshold alerts on drift (that belongs to the watcher's
  zero-yield classes).
