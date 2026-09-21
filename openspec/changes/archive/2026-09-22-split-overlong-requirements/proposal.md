## Why

`openspec validate --specs` flags two requirements whose prose runs past the 500-character guidance — a requirement is meant to be one testable contract, and these each carry three or four unrelated normative statements in a single paragraph:

- **`stats-reporting` → "Fast summary by default" (882 chars)**: mixes the summary's field list, a naming rule for the stored-row figure, the by-construction agreement with `coverage_report`, and a latency budget. A reader cannot tell which sentence a failing test would falsify.
- **`wbgapi-datasource` → "wbgapi adapter registered at adapters import" (705 chars)**: mixes the param shape the adapter must produce with the lazy-import rule that keeps registration safe without `wbgapi` installed. The second statement is why the second scenario exists, but nothing in the requirement's opening says so.

Both are documentation-shape problems, not behavior problems: every normative statement is already implemented, tested, and (for `stats-reporting`) verified live through the public edge.

## What Changes

- **`stats-reporting`**: "Fast summary by default" keeps its payload contract and both of its scenarios (a MODIFIED block replaces the whole requirement, so its scenarios stay put); the stored-row naming rule and the `coverage_report` agreement rule move out into requirements of their own. Each new requirement carries a scenario asserting something its old neighbour did not: the agreement rule now pins that the figures are *read from* the aggregation rather than counted twice (so they track a state change), and the naming rule gains the multi-source counting case (already asserted by `tests/test_data_stats_summary.py::test_stored_rows_count_sources_separately_from_points`).
- **`wbgapi-datasource`**: the single requirement keeps its param-shape contract and both scenarios; the lazy-import rule becomes its own requirement, with a scenario on the import graph itself (neither optional dependency is pulled in as a side effect of importing the module).

**No normative change.** Every pre-existing scenario's WHEN/THEN and every field name survives verbatim (checked mechanically — see task 2.1), and every SHALL statement survives in content; four sentences are reworded in form, one of them deliberately strengthened: the agreement rule now forbids counting the summary's coverage figures by a second, independent path, which is how the implementation already behaves. The split only regroups the prose and adds the two scenarios named above. No code, no schema, no deploy.

## Capabilities

### New Capabilities
<!-- None. -->

### Modified Capabilities
- `stats-reporting`: requirement text repartitioned (1 requirement → 3), no behavior change.
- `wbgapi-datasource`: requirement text repartitioned (1 requirement → 2), no behavior change.

## Impact

- **Specs only**: `openspec/specs/stats-reporting/spec.md` and `openspec/specs/wbgapi-datasource/spec.md`. No file under `fd_open_data_mcp/` or `tests/` is touched, and nothing is redeployed.
- **Effect**: `openspec validate --specs` reports zero warnings and zero INFO notes for this repository (was 2 INFO).
- **Out of scope**: the ~15 other `>500 characters` INFO notes the memory records for a different repository's spec set — this change covers only the two in `fd-open-data-mcp`.
