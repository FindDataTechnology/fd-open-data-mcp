# Data retention runbook

Operational procedures for `data_retention_contracts` and everything that enforces it.
The *history and reasoning* live in the archived change
`openspec/changes/archive/2026-11-12-establish-data-retention-principle/decisions-2026-11-11-phase1.md`
(this repo's sibling workspace). This file is only the **procedure** — what to do, in what
order, and how to tell whether it worked.

## 1. The model in one paragraph

`data_retention_contracts` is the **only** source of truth: one row per `(source, family)`
declaring a class, and for windows a length in days. Four classes, each answering a
different question:

| Class | Means | Deletes? |
|---|---|---|
| **P** | permanent (irrecoverable or our own product) | never |
| **F** | full history (upstream statistics that revise history; as-of needs it) | never |
| **W** | rolling window of N days | rows older than N days |
| **T** | only the current period | every point except the newest per key |

**The floor is 1826 days (5 years) for every W family.** A W contract shorter than that is a
bug, not a preference — it was the stated retention ruling, and the short values that shipped
originally were bootstrap defaults nobody had checked.

## 2. Where it is enforced

| Path | Who | What it does |
|---|---|---|
| write path (package) | `fetch/retention_bulk.apply_bulk_retention` | after an insert, in the **same transaction**: W delete + T keep-latest, one batch revision per merged entity |
| write path (crawler) | `scraw-fd-open-data-mcp` `db.apply_retention` called from `close_spider` | same call, once per job |
| offline sweep | `scripts/evict_retention_window.py --apply` | moves W-over-window / T-superseded rows into `semantic_observations_quarantine` |
| observability | `scripts/audit_retention_contracts.py` | read-only; reports capacity violations, contract gaps, and `T_class_accruing_history` |

The offline sweep runs daily as the CronJob `fd-master/retention-audit` (`20 5 * * *`
Asia/Shanghai): audit first, then evict, then a quarantine-size report. It is the **backstop**
for every writer we do not own (other repos' extractors).

## 3. Changing a window safely (four steps — skipping one is how data disappears)

1. **Write the rollback point first.** One line per row: current class + `retention_window_days`.
2. **Widen or narrow deliberately.** Narrowing is irreversible; widening is reversible. If the
   matrix in `scripts/audit_retention_contracts.py` disagrees with the DB, fix the **matrix
   first** — `seed_retention_contracts.py --overwrite` rewrites from it, and it refuses to
   narrow without `--allow-narrowing`.
3. **Move rows back in if the window got wider.** A contract change does not resurrect rows;
   they sit in quarantine. Move back the ones that are now legal, in one transaction, keeping
   only ids that actually inserted:

   ```sql
   WITH cand AS (
       SELECT id, concept_id, entity_type, entity_id, date, granularity, value, unit,
              source_used, fetched_at, obs_month
         FROM semantic_observations_quarantine
        WHERE batch_id = :batch AND length(date) = 10
          AND date >= to_char(now() - (:days || ' days')::interval, 'YYYY-MM-DD')
   ), ins AS (
       INSERT INTO semantic_observations (id, concept_id, entity_type, entity_id, date,
              granularity, value, unit, source_used, fetched_at, obs_month)
       SELECT id, concept_id, entity_type, entity_id, date,
              granularity, value, unit, source_used, fetched_at, obs_month FROM cand
       ON CONFLICT (id) DO NOTHING
       RETURNING id
   )
   DELETE FROM semantic_observations_quarantine q USING ins WHERE q.id = ins.id
   RETURNING q.id;
   ```

   `ON CONFLICT (id) DO NOTHING` + deleting only the returned ids is the whole point: a row
   that failed to insert must stay parked, otherwise you have "restore failed *and* the
   original is gone".

4. **Verify with the four criteria in §5** — do not trust the absence of errors.

## 4. Adding a new family

1. Decide the class with the three questions (does upstream revise history / how deep does the
   analysis need / how expensive is a re-fetch). Never guess: an undeclared family is `unknown`,
   and `unknown` is not "safe to delete".
2. Insert the contract (**and** the matching row in `DEFAULT_CONTRACTS`).
3. Make every writer of that family apply retention: call
   `apply_bulk_retention(..., concept_ids=[...])` in the same transaction as the insert, or
   route through `fetch.cache`. Static guards exist in **both** repos —
   `tests/test_bulk_retention_guard.py` here and
   `scraw-fd-open-data-mcp/tests/test_retention_bypass_guard.py` in the crawler — and each
   scans `INSERT INTO semantic_observations` without a retention call, which is how a missed
   writer is supposed to be caught. A guard only scans its own tree, so "one repo has a guard"
   proves nothing about the other.
4. Confirm `audit_retention_contracts.py` reports `契约待补 0`.

## 5. The five verification criteria (master-local — see the shard caveat below)

```sql
-- (a) no same-date duplicates. The key MUST include source_used: the same point written by
--     two sources is by design, not a duplicate.
SELECT count(*) FROM (
  SELECT concept_id, entity_type, entity_id, granularity, source_used,
         count(*) n, count(DISTINCT date) d
    FROM semantic_observations GROUP BY 1,2,3,4,5) x WHERE n > d;          -- expect 0

-- (b) no W row outside its window
SELECT ct.source, ct.family, count(*)
  FROM data_retention_contracts ct
  JOIN concepts co ON split_part(co.code,'.',1) = ct.family
  JOIN semantic_observations o ON o.concept_id = co.id AND o.source_used = ct.source
 WHERE ct.retention_class = 'W' AND length(o.date) = 10
   AND o.date < to_char(now() - (ct.retention_window_days || ' days')::interval, 'YYYY-MM-DD')
 GROUP BY 1,2;                                                           -- expect 0 rows

-- (c) every T key has exactly one date
SELECT max(n) FROM (
  SELECT concept_id, entity_type, entity_id, granularity, source_used, count(DISTINCT date) n
    FROM semantic_observations GROUP BY 1,2,3,4,5) x
 WHERE concept_id IN (SELECT ...T concepts...);                          -- expect 1

-- (d) quarantine holds nothing that is now legal. Run per batch, against the CURRENT contract
--     and PER CLASS. W and T disagree on what "legal" means:
--       W: legal = inside the window.
--       T: legal = inside the window AND the newest date on its key. An older date inside the
--          window is *supposed* to be evicted — that is the whole difference between T and W.
--     Skipping the class filter makes ~320k legitimate T evictions look like a 320k-row mistake.

-- (e) every (source, family) that holds data has a contract. Resolve the WILDCARD before
--     reporting a gap: a contract with family='*' covers every family of that source.
SELECT count(*) FROM (
  SELECT o.source_used, split_part(co.code,'.',1) fam
    FROM semantic_observations o JOIN concepts co ON co.id = o.concept_id
   GROUP BY 1,2) p
 WHERE NOT EXISTS (SELECT 1 FROM data_retention_contracts ct
                    WHERE ct.source = p.source_used
                      AND ct.family IN (p.fam, '*')
                      AND ct.retention_class IS NOT NULL);            -- expect 0
```

`reason='crawler_write'` in `observation_revisions` is the marker that the crawler's write path
actually ran a merge (batch ids are always `bulk-consolidate-*`, so the id cannot be used).

**The `family='*'` row is a whole-source fallback, and forgetting it manufactures a false gap.**
On 2026-11-13 an ad-hoc join on exact family equality reported 22 "data but no contract" pairs
(43k rows in the largest). Every one of them had a `(`source`, `*`, F)` row: the source had been
blanket-declared full-history and never split per family. Read the other way, the same query is
also how you find families that are governed only by a blanket "keep everything" decision — those
22 pairs (all F) have never been looked at individually, which is legal but worth knowing.

Current reading (2026-11-13): 107 data-bearing pairs = 85 exact contracts + 22 wildcard-only
(all F) + **0 truly ungoverned**. That matches the audit's own "共 107 个 (源, 族)".

**Every criterion above — and the audit itself — sees `semantic_observations` on the MASTER
only.** Shard-hosted families are written through foreign tables (`ft_sem_obs_<shard>_w`) and
are invisible here: `astock_daily` alone holds 48,316,490 rows ≈16 GB on xinru3 while the master
reports 0 rows for it, so it is neither reported as `no_contract` nor evicted. Whenever you state
"no gaps database-wide", name the surface you scanned. Shard coverage is an open item (`F13` in
the archived change), not a solved one.

**Pick the right federated view — two names differ by one word and by a whole shard** (verified
2026-11-13 with `pg_get_viewdef`):

| view | unions |
|---|---|
| `semantic_observations_all` | `ft_sem_obs_xinru3` **only** |
| `semantic_observations_read` | `ft_sem_obs_xinru2` **+** `ft_sem_obs_xinru3`, deduped with astock_daily priority |

Shard split: **astock_daily daily bars live on xinru3; akshare daily bars and *all* financials
live on xinru2.** So `_all` silently drops an entire shard — using it as "the whole database"
is barely better than the master alone. Use `semantic_observations_read`, and expect it to be
slow (250 partitions unioned with two foreign tables).

**How much of the corpus this actually governs (measured 2026-11-13, per surface):**

| surface | dominant sources | rows | audit sees it | eviction reaches it |
|---|---|---|---|---|
| master `semantic_observations` | 107 (source, family) pairs | 1,042,939 | yes | yes |
| `ft_sem_obs_xinru2` | `akshare` 19,559,474 / `akshare_misbound` 561,141 | 20,120,615 | no | no |
| `ft_sem_obs_xinru3` | `astock_daily` 48,316,490 | 48,316,490 | no | no |
| **total** | | **≈69,480,044** | **1.5%** | **1.5%** |

Read that as: **the contract is a declaration; enforcement covers 1.5% of the corpus, and the
two can differ by 65×.** akshare's 19.6M shard rows *do* have contracts (so for them the gap is
enforcement, not declaration); `astock_daily` only got one on 2026-11-13 (gap was both). Any
"the window is enforced" claim must say which surface it means.

**The daily CronJob now prints all four surfaces**, so none of this has to be re-measured by
hand: section `4/4 分片体量` reports each shard separately (own connection,
`statement_timeout=240s`, `|| true` — a shard being down is the shard's problem, not the job's
verdict), and section `1/2` is labelled "范围＝master 本地" so nobody reads it as whole-database.
Current shard reading: xinru2 20,120,615 (`akshare` 19,559,474 / `akshare_misbound` 561,141),
xinru3 48,316,490 (`astock_daily`) — watch the trend, not the absolute.





**Quarantine baseline (measure before calling growth a leak).** First reading on 2026-11-13 was
1,725,302 rows ≈ 584 MB, which looks alarming. Split by day it is one historical bill plus a
trickle:

| day | rows into quarantine | T | W |
|---|---|---|---|
| 2026-11-10 | 1,363,140 | 1,021,677 | 341,463 |
| 2026-11-11 | 10,372 | 10,372 | 0 |
| 2026-11-12 | 40 | 40 | 0 |
| 2026-11-13 | 0 | 0 | 0 |

≈3.5 MB/day afterwards. The quarantine has **no expiry by decision** (that is what makes §6's
last row true), so the daily report's §3 is the only thing that will ever tell you it sped up.


## 6. Rollback recipes

| What | How |
|---|---|
| window too short | update the contract back, then re-run the §3.3 restore for that batch |
| wrongly evicted rows | re-run §3.3 with `date >= <cutoff>` of the old window; rows keep their original ids |
| crawler fleet on a bad image | `UPDATE clusters SET image='...:<old-sha>'` — the fleet reads that row, **not** the manifest |
| evicted then need the bytes back? | the quarantine is the only copy; nothing purges it, and that is deliberate |

## 7. Seven traps that have actually bitten

1. **The fleet's image is a DB row.** `clusters.image` is what dispatched Jobs run; the
   `SCRAW_K8S_IMAGE` env in the reconciler manifest is only the single-cluster default. Editing
   the manifest does not roll anything.
2. **A second copy of the contracts will drift.** `DEFAULT_CONTRACTS` in
   `audit_retention_contracts.py` is a bootstrap matrix, not the truth. After any manual contract
   edit, align it — otherwise the next `seed --overwrite` silently reverts you (it can move
   ~250k rows at exit code 0; hence `narrowing_changes()`).
3. **The quarantine table has two writers.** `retention-*` batches come from window eviction;
   everything else comes from misattached-row isolation. Report them separately or the classes
   in one look like they were evicted by the other.
4. **Calibers must match the unique key.** The key is
   `(concept_id, entity_type, entity_id, date, granularity, source_used)`. Any "per key" query
   that drops `source_used` reports multi-source coexistence as duplicates; any T victim
   selection that drops `granularity` deletes monthly rows in favour of daily ones.
5. **"Move first, delete later" is not optional.** It is what makes every decision in this file
   reversible. Turning eviction into a `DELETE` would make §3.3 impossible and §6's second row
   a lie.
6. **`date` is varchar, and both enforcement paths compare it as a string.** W evicts with
   `o.date < to_char(now() - interval 'N days','YYYY-MM-DD')`; T merges with
   `ORDER BY o.date DESC`. String order equals time order only for fixed-length ISO dates.
   A `LATEST` in W is never evicted; in T it outranks every real date and displaces the genuine
   current row. Both silent. Measured 2026-11-13: W (918,629 rows) and T (28,233) are 100%
   `YYYY-MM-DD`, while P and F legitimately hold 23,721 non-ISO values (`1961`, `2026-10`,
   `2026-10-13T22:00Z`, `LATEST`) — so the audit now reports `date_not_iso` as a capacity
   violation for T/W only. Before trusting any date query, `\d+` the column.
7. **A relay tag can be "in the ledger" and still not exist.** `tcr-relay-sync.sh` copies only
   `main`, `anchors.conf` entries, and *first-seen* tags; `--seed` records tags **without**
   copying. A tag that was seeded before it was copied is therefore skipped by every full
   round forever, while the log happily shows other tags' `OK` lines. The only valid criterion
   is `skopeo inspect ccr:<ns>/<repo>:<tag>` returning a digest — `manifest unknown` means
   backfill it by hand: `tcr-relay-sync.sh <ns>/<repo> <tag>` (a non-empty tag argument skips
   all filters), then inspect again.

## 8. Exit codes (they decide whether anyone notices)

`audit_retention_contracts.py`: `0` clean / `1` capacity violation / `3` contract gaps only /
`2` unknown-or-failed. `evict_retention_window.py`: `0` nothing to do / `1` has work (dry-run)
or done (`--apply`) / `2` failure.

The CronJob maps `evict 1` and `audit 3` to success — otherwise it would be red every day and
nobody would read it — and keeps `audit 1` and `audit 2` red, because "unknown is not a pass".
`scraw-fd-open-data-mcp/tests/test_retention_cronjob_exit_codes.py` pins all eight combinations
against the real manifest text (it parses the CronJob YAML and drives it with a `python` shim, so
the test exercises the shipped text rather than a copy of it).

## 9. After editing the CronJob: run *that* version once

A `Complete 1/1` in the job history proves *some* version ran. Check which:

```bash
git log -1 --format=%cd -- k8s/retention-audit-cronjob.yaml
kubectl -n fd-master get cronjob retention-audit \
  -o jsonpath='{.status.lastScheduleTime}{"\n"}'
```

If the manifest is newer than `lastScheduleTime`, the last run was the previous text — the changes
have never executed. Run the current text once, read the log, then delete the job so it does not
eat a slot in `successfulJobsHistoryLimit`:

```bash
kubectl -n fd-master create job retention-audit-verify-$(date +%H%M) --from=cronjob/retention-audit
kubectl -n fd-master logs job/retention-audit-verify-<HHMM>
kubectl -n fd-master delete job retention-audit-verify-<HHMM>
```

Read all three sections, not just the exit code: (1) audit's per-class table, (2) evict's "refused"
lines — a family whose re-fetch cost exceeds the budget must refuse and name the hours, never
evict quietly, (3) the §3 quarantine report. A suspiciously short runtime is a clue: 12s for
"audit 107 families + evaluate 17" was the signature of a different, older code path.

