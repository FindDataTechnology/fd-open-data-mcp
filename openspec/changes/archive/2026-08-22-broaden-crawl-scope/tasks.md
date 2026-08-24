## 1. Land the uncommitted code streams

- [x] 1.1 Stage + commit the wbgapi adapter: `fd_open_data_mcp/adapters/wbgapi.py` (new) + the `adapters/__init__.py` registration block. (commit 2a209c3)
- [x] 1.2 Stage + commit the `add-proxy-service` leftover cleanup: `proxy/selector.py` (stripped `FD_PROXY_POOL`/`FD_EGRESS_MODE`/`SCRAW_CLUSTER_ID` branches), `refresh/reconciler.py` (env injection removed), `tests/test_multi_cluster.py` (ships-dark contract), `CONFIG.md`, `README.md`, `README.zh-CN.md`. (commit ec03fb5)
- [x] 1.3 Verify ships-dark locally: `cd fd-open-data-mcp && uv run pytest tests/test_multi_cluster.py -v` green (9 passed); `FD_PROXY_FORWARDER` unset → `acquire` returns direct sentinel, no regression.
- [x] 1.4 Run the wbgapi adapter unit check: import `fd_open_data_mcp.adapters` in an env WITHOUT the `wbgapi` package installed → no `ModuleNotFoundError`; `get_indicator_data` registered (wbgapi not installed, confirmed in registry list).

## 2. Bump version + image build args

- [x] 2.1 Bump `fd_open_data_mcp/__init__.py` `__version__` `0.4.7` → `0.4.8`.
- [x] 2.2 In `scraw-fd-open-data-mcp/Dockerfile` bump `ARG FD_ODM_INSTALL="fd-open-data-mcp>=0.4.8"`.
- [x] 2.3 In `scraw-fd-open-data-mcp/.github/workflows/cicd.yml` bump the build-args `FD_ODM_INSTALL=fd-open-data-mcp>=0.4.8`.
- [x] 2.4 Build fd-open-data-mcp 0.4.8 wheel: `cd fd-open-data-mcp && uv build` → `twine check dist/*` clean (both PASSED).

## 3. Publish fd-open-data-mcp 0.4.8 to PyPI

- [x] 3.1 Tag + push: `cd fd-open-data-mcp && git tag v0.4.8 && git push --tags` (release.yml OIDC trusted-publishing builds + uploads).
- [x] 3.2 Confirm `pip index versions fd-open-data-mcp` shows 0.4.8 (or `pip install --dry-run fd-open-data-mcp==0.4.8`).

## 4. Rebuild + push the scraw worker image

- [x] 4.1 Commit + push the `scraw-fd-open-data-mcp` Dockerfile/cicd.yml bumps to main → triggers `cicd.yml` build-and-push.
- [x] 4.2 Confirm the GH Actions run succeeded (image pushed to `23.144.68.246:30880/lawcraw_business/scraw-fd-open-data-mcp:latest`).
- [x] 4.3 Pull-based deploy confirmation: on xinru, the next reconciler tick (≤15 min) or next crawl Job auto-pulls `:latest` (imagePullPolicy: Always). No `kubectl apply` needed. Verify with `sudo kubectl -n scraw get pods` showing the new pod's image digest.

## 5. Enumerate WDI indicators + seed bindings

- [x] 5.1 Run `enumerate_wbgapi_indicators` MCP tool (or equivalent) against the canonical xinru DB to upsert WDI `columns` + `concepts` + `concept_bindings` rows. (k8s Job wbgapi-enumerate: imported 1498/1498, 0 errors)
- [x] 5.2 Confirm via `psql`: `SELECT count(*) FROM columns WHERE source='wbgapi'` and `SELECT count(*) FROM concept_bindings WHERE ... wbgapi ...` show non-zero. (columns=1510, concepts=1506)

## 6. Run a wbgapi crawl policy

- [x] 6.1 Create a wbgapi crawl policy: `wbgapi-wdi-probe` (id=6), concept_ids [1446 NY.GDP.MKTP.CD, 1450 NY.GDP.MKTP.KD.ZG, 1455 NY.GDP.PCAP.CD, 2111 SP.POP.TOTL], `entity_type='country'`, entity_ids [1 CN, 2 US, 3 JP, 6 DE, 10 IN], `date_policy={mode: explicit, start 2020-01-01, end 2024-01-01}`, `mode='per_date'` (series mode refused: `get_indicator_data` has `bulk_history=f`; per_date is the working mode), `source_filter=["wbgapi"]`, `cron_expr='0 6 * * *'`, `frequency='yearly'`. (direct psql INSERT; MCP `policy_create` can't reach xinru from Mac)
- [x] 6.2 Trigger the crawl: manual reconciler Job `reconcile-manual-wbgapi-2` in fd-master ns launched policy 6 on cluster aliyun as `crawl-policy-6-20260822033908`, estimate=100. (launched run id=66)
- [x] 6.3 Monitored the crawl Job: 100 fetch_log rows all `status=ok` (25 per concept × 4 concepts); Job completed ~03:47 UTC. (reconciler run still shows `running` until the next 15-min tick probes the aliyun job closed — data already landed)
- [x] 6.4 Verify WDI rows landed: `SELECT count(*) FROM semantic_observations WHERE source_used='wbgapi'` = 1250 (was 1150; +100 new = 4 concepts × 5 countries × 5 years 2020-2024, all `ok`).

## 7. OpenSpec validation

- [x] 7.1 `openspec validate broaden-crawl-scope` → valid, 0 issues. (run from `fd-open-data-mcp/` openspec root; the root `finddata/` dir is not an openspec project so `openspec` there reports "Unknown item" — the change lives under `fd-open-data-mcp/openspec/changes/`).
- [x] 7.2 Credential grep: `grep -rn GostEgress2026x9k` over source `.py` files (excluding `openspec/` planning dirs) → exit 1, empty (no regression from G1 scrub). The string survives only in this tasks.md task description itself, never in code; `_DEFAULT_EGRESS_AUTH` has 0 matches in the live `proxy/seed.py`.
