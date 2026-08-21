## Why

The live cluster runs the 0.4.7 image, which predates two uncommitted streams in
`fd-open-data-mcp`: the **wbgapi adapter** (`adapters/wbgapi.py` + the
`adapters/__init__.py` registration) and the **`add-proxy-service` cleanup**
(legacy `FD_PROXY_POOL` / `FD_EGRESS_MODE` / `SCRAW_CLUSTER_ID` branches stripped
from `proxy/selector.py` + `refresh/reconciler.py`). Without the wbgapi adapter,
the scraw `fetch_handler` legacy fallback builds `{symbol, date}` params, which
`run_wbgapi` rejects ("needs indicator + economy") — so WDI concepts can't be
crawled end-to-end even though the `wbgapi` provider, seed, mapper, and
`enumerate_wbgapi_indicators` MCP tool all already exist. Landing the adapter
behind an image rebuild + pull-based deploy unlocks crawling WDI (World Bank)
data, broadening the crawl scope beyond the akshare/EDGAR/Datacommons sources
already populated.

## What Changes

- Ship the **wbgapi adapter**: `adapters/__init__.py` loads
  `adapters/wbgapi.py` at import so `fetch_handler` maps `economy`<-resolved
  entity id, `indicator`<-binding column name (WDI series code), `date`<-year,
  instead of the broken legacy `{symbol, date}` fallback.
- Ship the **`add-proxy-service` leftover cleanup**: remove dead
  `FD_PROXY_POOL` / `FD_EGRESS_MODE` / `SCRAW_CLUSTER_ID` branches from the
  legacy in-process `proxy/selector.py` and the reconciler's Job-env injection
  (the crawl hot path already uses `fd-proxy-service` via `FD_PROXY_FORWARDER`;
  these branches were unreachable). Update `test_multi_cluster.py` to the
  ships-dark-no-proxies contract.
- **Bump** `fd-open-data-mcp` `0.4.7` -> `0.4.8` and the scraw Dockerfile /
  `cicd.yml` build-arg `FD_ODM_INSTALL=fd-open-data-mcp>=0.4.8`.
- **Rebuild + push** the `scraw-fd-open-data-mcp` image (push `:latest` to
  Harbor IS the deploy — `imagePullPolicy: Always`; the reconciler CronJob +
  new crawl Jobs auto-pull it).
- **Run more crawls**: enumerate WDI indicators + run a wbgapi crawl policy so
  WDI concepts land in `semantic_observations` on the canonical xinru DB.

## Capabilities

### New Capabilities
<!-- None. -->

### Modified Capabilities
- `wbgapi-datasource`: ADD a requirement that the adapters package SHALL
  register a `get_indicator_data` adapter at import so the scraw `fetch_handler`
  produces `{economy, indicator, date}` (not the legacy `{symbol, date}`
  fallback), making WDI concepts crawlable end-to-end through the existing
  concept-crawl executor.

## Impact

- **Code**: `fd-open-data-mcp` — `fd_open_data_mcp/adapters/__init__.py`,
  `fd_open_data_mcp/adapters/wbgapi.py` (new), `fd_open_data_mcp/proxy/selector.py`,
  `fd_open_data_mcp/refresh/reconciler.py`, `fd_open_data_mcp/__init__.py`
  (version bump), `tests/test_multi_cluster.py`, `CONFIG.md`, `README*.md`.
- **Image**: `scraw-fd-open-data-mcp` Dockerfile + `.github/workflows/cicd.yml`
  build-arg bump to `fd-open-data-mcp>=0.4.8`; rebuilt image pushed to
  `23.144.68.246:30880/lawcraw_business/scraw-fd-open-data-mcp:latest`.
- **Data**: new WDI rows in `semantic_observations` (canonical xinru,
  `134.175.46.69:30432`) via `enumerate_wbgapi_indicators` + a wbgapi crawl
  policy.
- **No breaking changes**: the proxy cleanup removes only unreachable branches;
  ships-dark (`FD_PROXY_FORWARDER` unset) is unchanged (direct sentinel egress).
