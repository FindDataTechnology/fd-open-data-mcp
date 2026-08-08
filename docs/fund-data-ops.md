# Fund data operations (add-fund-crawl-control-center)

Operational runbook for the fund/person ontology on the live DB
(`192.168.1.4:5433/postgres`). Seeding scripts are idempotent; the *edge
cases* below are the manual interventions.

## Four-slot discipline

Every akshare fund column is sorted into **exactly one** slot before it
becomes anything (design D3). When adding a new fund indicator, decide its
slot first:

| Slot | Examples | Where it lives |
|---|---|---|
| **dated scalar observation** | 净值, 规模, 阶段收益, 7日年化 | concept (seed via `seed_fund_concepts.py`) + `semantic_observations` (crawled by a policy) |
| **entity attribute** | 成立日, 托管人, 费率, 类型 | `entities.metadata_json` (seed/enrichment script, NOT a concept) |
| **entity↔entity edge** | managed_by, issued_by, tracks | `entity_relationships` (`build_fund_edges.py`) |
| **derived cross-section** | 同类排名 | not stored — computed on read |

Rules of thumb:

- If it's a time series → slot 1 (concept). If it's a static fact about the
  fund → slot 2 (metadata). If it connects two entities → slot 3. If it only
  makes sense *relative to peers* → slot 4, don't store it.
- Concept codes are `<namespace>.<measure>`: `nav.*`, `yield.*`, `price.*`
  (entity_type='fund' variants — distinct from the stock rows via the unique
  key), `return.*`, `aum`, `holder.*`, `rating.*`.
- Concepts are seeded directly (empty `source`) — do **not** run them through
  the `indicator_defs` consume pipeline (that's macro/country-shaped).
- Bind each new concept's columns via `seed_fund_bindings.py`, then confirm
  `plan_crawl` routes it (fund functions declare `real_sources`; bulk history
  functions set `bulk_history: true` in the catalog for `series` mode).

## Person merge runbook

The seed dedupes persons by `(姓名, 所属公司)` (design D2). Two unavoidable
false splits; fix with `scripts/merge_persons.py`:

- **跳槽 (job hop)**: the same human has two person entities under old/new
  company names. Merge the old one into the new.
- **重名 (namesake)**: two distinct humans share a name at one company (rare);
  the heuristic wrongly merged them. This is *not* fixable by merge_persons —
  you must split, which means re-seeding the loser from `fund_manager_em`
  (identifier hint = eastmoney 序号) and re-running `build_fund_edges.py`.
  Prefer leaving them merged unless the observations look wrong.

```bash
# Merge 张三@旧公司 INTO 张三@新公司 (first arg survives)
python scripts/merge_persons.py '张三@旧公司' '张三@新公司' --dry-run   # inspect
python scripts/merge_persons.py '张三@旧公司' '张三@新公司'            # apply
```

What the merge repoints (winner survives, loser deleted):
- `entity_relationships` source_id/target_id (colliding edges dropped)
- `entity_source_identifiers` (colliding identifiers → winner
  `metadata_json.merged_identifiers`)
- `semantic_observations` (colliding `(concept, date)` → winner's row wins)
- `metadata_json` fields union; `merged_from` records the loser code(s)

Person concepts (`aum_total`, `funds_count`, `tenure_days`, `best_return`)
recompute naturally from the merged relationships on the next crawl; delete the
loser's stale observations after merging (they now point at the winner).
