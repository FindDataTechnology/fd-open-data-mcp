# Tasks — split-overlong-requirements

## 1. Repartition the two long requirements

- [x] 1.1 Split `stats-reporting`'s "Fast summary by default" into three requirements — the payload + latency contract, the `coverage_report` agreement rule, and the stored-row unit-naming rule. The original requirement keeps both of its scenarios (a MODIFIED block replaces the whole block, so `openspec validate` refuses a drop); the two extracted rules get scenarios of their own. Verify: `openspec validate split-overlong-requirements --strict` passes; no requirement body exceeds 500 characters.
- [x] 1.2 Split `wbgapi-datasource`'s single requirement into the registration + param-shape contract and the environment-independence (lazy import) contract, the original keeping both its scenarios and the new one asserting the import graph. Verify: same validation, and both bodies are under 500 characters.

## 2. Prove the split is editorial

- [x] 2.1 Mechanically diff the before/after requirement text of both capabilities: every `SHALL` clause, field name (`total_concepts`, `covered_concepts`, `concepts_with_observations`, `total_stored_rows`, `{economy, indicator, date}`, `{symbol, date}`), and scenario WHEN/THEN from the pre-split main specs must survive verbatim in the merged result. Verify: the diff shows no lost or altered normative text; the only additions are the new requirement names and the one new scenario.
- [x] 2.2 Confirm no code or test change is needed: the new multi-source scenario describes behavior already asserted by `tests/test_data_stats_summary.py::test_stored_rows_count_sources_separately_from_points`. Verify: that test passes unchanged.
