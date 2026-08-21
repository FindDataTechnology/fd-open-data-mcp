## Context

`fd-open-data-mcp` has two uncommitted streams in its working tree that the
live cluster can't use because the scraw image still pins `fd-open-data-mcp>=0.4.7`
(the version **before** either stream landed):

1. **wbgapi adapter** (`adapters/__init__.py` +8, `adapters/wbgapi.py` new 69
   lines) — maps the concept-crawl `fetch_handler` to `run_wbgapi`'s
   `{economy, indicator, date}` contract. Without it, the legacy fallback builds
   `{symbol, date}`, which `run_wbgapi` rejects — so WDI concepts are uncrawlable
   even though the `wbgapi` provider, seed, mapper, `enumerate_wbgapi_indicators`
   MCP tool, and `wbgapi-datasource` spec all already exist.
2. **`add-proxy-service` leftover cleanup** (`proxy/selector.py` +4−35,
   `refresh/reconciler.py` +8−16, `tests/test_multi_cluster.py` +6−20,
   `CONFIG.md`) — strips the dead `FD_PROXY_POOL` / `FD_EGRESS_MODE` /
   `SCRAW_CLUSTER_ID` branches from the legacy in-process selector + the
   reconciler's Job-env injection. The crawl hot path already routes through
   `fd-proxy-service` via `FD_PROXY_FORWARDER`; these branches were unreachable.

Deployment is **pull-based**: pushing `:latest` to Harbor IS the deploy
(`imagePullPolicy: Always`). The reconciler CronJob (`7,22,37,52 * * * *`) and
new crawl Jobs auto-pull. No `kubectl apply` / rollout needed. Canonical DB is
xinru (`134.175.46.69:30432`, mesh `100.64.0.3`), 144M `semantic_observations`.

## Goals / Non-Goals

**Goals:**
- Land both uncommitted streams behind one version bump + image rebuild.
- Prove WDI data is crawlable end-to-end (adapter → fetch_handler → run_wbgapi
  → `semantic_observations` rows on xinru).
- Keep ships-dark behavior (`FD_PROXY_FORWARDER` unset → direct sentinel
  egress) unchanged by the proxy cleanup.

**Non-Goals:**
- New proxy-service behavior (the `fix-proxy-circuit-classification` fixes
  already landed in 0.4.7; this change ships no new proxy logic).
- WDI historical backfill at scale — only a small policy proving the pipeline.
- Changes to `fd-proxy-service` (clean working tree, no bump needed).

## Decisions

### D1 — One change, one version bump (0.4.7 → 0.4.8)

Both streams are already written and uncommitted; bundling them into a single
0.4.8 release minimizes image rebuilds. The wbgapi adapter is the load-bearing
piece; the proxy cleanup rides along (it removes only unreachable code, so it
can't break the crawl path).

**Alternative considered:** ship the wbgapi adapter alone, defer the proxy
cleanup. Rejected — the cleanup is already done and reviewed; splitting it
creates a second image rebuild for zero functional benefit.

### D2 — Adapter registered unconditionally at adapters import

`adapters/__init__.py` does `from fd_open_data_mcp.adapters import wbgapi as
_wbgapi_adapters`. The adapter module has **no top-level third-party imports**
(`pandas` and `wbgapi` are imported lazily inside `extract_value`/`extract_series`),
so it is safe in every environment that imports the adapters package — including
the scraw worker before the `data` extra is installed. Mirrors the
`akshare.py` side-effect registration pattern.

**Alternative considered:** lazy-register on first wbgapi dispatch. Rejected —
the akshare precedent is import-time registration; deviating would make the
adapter plumbing inconsistent.

### D3 — Image rebuild via the existing cicd.yml, no Dockerfile edits beyond the build-arg

The `scraw-fd-open-data-mcp` Dockerfile already installs `wbgapi>=1.0` and pins
`scrapy>=2.12,<2.13` + `Twisted<25` + `akshare>=1.17`. The only change needed is
bumping the `FD_ODM_INSTALL` build-arg in both `Dockerfile` (line 23) and
`.github/workflows/cicd.yml` (line 95) from `>=0.4.7` to `>=0.4.8`, then
pushing to the `scraw-fd-open-data-mcp` repo `main` branch to trigger the
workflow. The `:latest` tag push IS the deploy.

**Alternative considered:** manual `docker build` + `docker push` from a Mac.
Rejected — the GH Actions path is the canonical one (proven against the HTTP-only
Harbor); manual pushes bypass the build-arg pinning and the insecure-registry
daemon config that the workflow handles.

### D4 — WDI crawl via `enumerate_wbgapi_indicators` + a small crawl policy

To prove the adapter: run the `enumerate_wbgapi_indicators` MCP tool to bind
WDI indicator codes to concepts (idempotent), then create a `crawl_policies`
row targeting a few macro concepts (e.g. `gdp.current`, `population.total`)
for a handful of countries with `date_policy: {mode: trailing, days: 3650}`
(10-year window). The reconciler picks it up on the next 15-min tick, compiles
a CrawlPlan, and launches a crawl Job that auto-pulls the new image.

## Risks / Trade-offs

- **[wbgapi lazy-import ordering]** If a future edit adds a top-level `import
  wbgapi` to the adapter module, the unconditional import in
  `adapters/__init__.py` breaks every environment without the `data` extra.
  → Mitigation: the spec scenario "adapters package imports without wbgapi
  installed" guards this; the `# noqa: E402,F401` line comment flags it.
- **[Harbor push failure]** The GH Actions build can 401 on the blob HEAD if
  the robot account secret rotates. → Mitigation: the workflow already uses the
  `lawcraw_business` robot (proven 202 on blob upload); re-run on failure.
- **[WDI API reachability]** The wbgapi API (`api.worldbank.org/v2`) must be
  reachable from the crawl Job's egress. If the cluster IP is geo-blocked or
  rate-limited, the crawl produces 0 rows. → Mitigation: wbgapi is a free
  no-auth API, historically reachable; if it fails, the policy row still
  records a `policy_runs` failure for diagnosis (no silent loss).
- **[proxy cleanup removes only dead branches]** If a caller still sets
  `FD_PROXY_POOL` expecting the legacy selector to read it, the cleanup is a
  silent no-op (the var is ignored). → Mitigation: `CONFIG.md` update
  documents the removal; ships-dark is the only supported local config.

## Migration Plan

1. Commit + push the wbgapi adapter + proxy cleanup + version bump to
   `fd-open-data-mcp` main → tag `v0.4.8` → GitHub Actions publishes 0.4.8 to PyPI.
2. Bump `FD_ODM_INSTALL` in `scraw-fd-open-data-mcp` `Dockerfile` + `cicd.yml`
   → push to `main` → GH Actions rebuilds + pushes `:latest` to Harbor.
3. (Pull-based deploy) — no kubectl. The next reconciler tick (≤15 min) and new
   crawl Jobs auto-pull `:latest`.
4. Run `enumerate_wbgapi_indicators` + create a wbgapi crawl policy → confirm
   WDI rows in `semantic_observations` on xinru.

**Rollback:** the image is tagged with the commit SHA in addition to `:latest`;
pin a crawl Job's image to the prior SHA tag to force the old image. Or revert
the `FD_ODM_INSTALL` build-arg to `>=0.4.7` and re-push. The wbgapi adapter is
additive (only fires for `wbgapi` provider dispatch); reverting the image
restores the pre-adapter `{symbol, date}` fallback behavior with no data loss.
