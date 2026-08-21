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

- [ ] 3.1 Tag + push: `cd fd-open-data-mcp && git tag v0.4.8 && git push --tags` (release.yml OIDC trusted-publishing builds + uploads).
- [ ] 3.2 Confirm `pip index versions fd-open-data-mcp` shows 0.4.8 (or `pip install --dry-run fd-open-data-mcp==0.4.8`).

## 4. Rebuild + push the scraw worker image

- [ ] 4.1 Commit + push the `scraw-fd-open-data-mcp` Dockerfile/cicd.yml bumps to main → triggers `cicd.yml` build-and-push.
- [ ] 4.2 Confirm the GH Actions run succeeded (image pushed to `23.144.68.246:30880/lawcraw_business/scraw-fd-open-data-mcp:latest`).
- [ ] 4.3 Pull-based deploy confirmation: on xinru, the next reconciler tick (≤15 min) or next crawl Job auto-pulls `:latest` (imagePullPolicy: Always). No `kubectl apply` needed. Verify with `sudo kubectl -n scraw get pods` showing the new pod's image digest.

## 5. Enumerate WDI indicators + seed bindings

- [ ] 5.1 Run `enumerate_wbgapi_indicators` MCP tool (or equivalent) against the canonical xinru DB to upsert WDI `columns` + `concepts` + `concept_bindings` rows.
- [ ] 5.2 Confirm via `psql`: `SELECT count(*) FROM columns WHERE source='wbgapi'` and `SELECT count(*) FROM concept_bindings WHERE ... wbgapi ...` show non-zero.

## 6. Run a wbgapi crawl policy

- [ ] 6.1 Create a wbgapi crawl policy: a small set of WDI concept_ids (e.g. GDP, population), `entity_type='country'`, `date_policy={mode: explicit, start, end}` or `since_last`, `mode='series'`.
- [ ] 6.2 Trigger the crawl: `policy_trigger_now` (or wait for the reconciler CronJob tick).
- [ ] 6.3 Monitor the crawl Job: `sudo kubectl -n scraw logs job/<name> --tail=50` until it completes.
- [ ] 6.4 Verify WDI rows landed: `psql -c "SELECT count(*) FROM semantic_observations WHERE source_used='wbgapi'"` is non-zero.

## 7. OpenSpec validation

- [ ] 7.1 `openspec validate broaden-crawl-scope` → 0 issues.
- [ ] 7.2 Credential grep: `grep -rn GostEgress2026x9k fd-open-data-mcp/ scraw-fd-open-data-mcp/` → empty (no regression from G1 scrub).
