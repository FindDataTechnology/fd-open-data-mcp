# Route yfinance through the proxy lane (its client ignores our patches)

## Why

yfinance is unusable from the fleet: **every direct egress is blocked**. Measured
2026-11-08 — `fd-deploy` (124.220.7.175) and `cheap3` answer HTTP 429 with a
23-byte `Edge: Too Many Requests` page, the seven cheap-cluster nodes answer 403
with a 3,369-byte page, and from fd-master `urllib`, `requests` and `curl_cffi`
(impersonating chrome124 / chrome / safari17_0 / nothing) are all 429. Waves that
depend on Yahoo pause themselves (`zero_yield`) and re-launch forever.

Rotating node IPs does not help — nine distinct egresses, two different block
shapes, no success. What does work is the **fleet proxy pool**: with browser
impersonation the same chart endpoint returns 200 through most pool proxies
(`curl_cffi impersonate="chrome124"` → 200 / 1,718 bytes on 27 of 45 tested; the
crumb endpoint also 200), while plain `urllib` gets 429 through the same proxies.

The pipeline already selects a proxy per fetch attempt — `instrumented_fetch`
acquires one from the forwarder and publishes it through
`injection.use_proxy(...)`, which monkey-patches `requests` and `httpx`. But
**yfinance 1.x talks over `curl_cffi`, which those patches never touch**, so it
silently egresses direct and dies on the block. Handing a prepared
`curl_cffi.Session(proxies=...)` to `yf.Ticker(session=...)` is also not enough:
yfinance opens its own cookie/crumb connections (`fc.yahoo.com`) which still fail
(`CONNECT tunnel failed, response 403`). The only lever that works is its global
config — `yf.set_config(proxy=...)`, verified to return real OHLCV rows through
the pool.

## What changes

- New `injection.current_proxy_url()` — a public read of the proxy the current
  attempt was given (the same contextvar the patches use).
- `run_yfinance` applies that proxy to yfinance before the call
  (`yf.set_config(proxy=...)`, falling back to `HTTP_PROXY`/`HTTPS_PROXY`) and
  restores the previous value afterwards, so one fetch's proxy cannot leak into
  the next.
- An explicit `FD_YF_PROXY` env override wins over the contextvar, for direct
  runs (probe job, backfills) that never go through `instrumented_fetch`.
- If no proxy is available the call proceeds direct (today's behaviour) rather
  than failing: a missing proxy must not turn a source into an error.

## Impact

- Affected specs: new capability `source-proxy-lane`.
- Affected code: `fd_open_data_mcp/fetch/runner.py` (`run_yfinance`),
  `fd_open_data_mcp/proxy/injection.py` (`current_proxy_url`), plus tests.
- Runtime: ships in a patch release and needs an image roll (the adapter lives
  in the package, not in `fd-scripts`).
- Caveat recorded: `yf.set_config` is process-global, so two concurrent yfinance
  fetches with different proxies can interleave. Crawler pods run one fetch at a
  time; callers that thread yfinance must not rely on per-call isolation.
- Not in scope: the price-crawl stop order — this repairs an existing lane's
  reachability, it does not add new price concepts.
