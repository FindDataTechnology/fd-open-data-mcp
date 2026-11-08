# 任务清单

## 1. 规格

- [x] 1.1 `openspec validate add-yfinance-proxy-lane --strict` 通过

## 2. 实现

- [x] 2.1 `proxy/injection.py` 增加 `current_proxy_url()`：返回当前 attempt 的代理 URL（contextvar 里 `https`/`http` 取一个），无代理返回 None
- [x] 2.2 `fetch/runner.py::run_yfinance`：调用前把该代理装进 yfinance（`yf.set_config(proxy=…)`，失败回退 `HTTP_PROXY`/`HTTPS_PROXY`），调用后恢复原值；`FD_YF_PROXY` 覆盖优先；无代理则不设、走直连

## 3. 测试

- [x] 3.1 单测：`current_proxy_url()` 在 `use_proxy(url)` 内返回该 URL、块外返回 None
- [x] 3.2 单测：`run_yfinance` 在代理就绪时调用 `set_config(proxy=…)` 且退出后恢复；无代理时不调用
- [x] 3.3 单测：`FD_YF_PROXY` 覆盖 contextvar

## 4. 验证与发布

- [x] 4.1 真网实测（pod 内装补丁版 `runner.py`/`injection.py`，同一进程先跑对照）：**直连对照组** `DIRECT ERR YFRateLimitError`；**设 `FD_YF_PROXY` 后** `PROXIED rows: 23`，末两行 2026-11-05/06 真实 OHLCV（344.17 / 342.37）。另举证：`set_config` 单独、`set_config`+env、env 单独三种写法均 5 行；直连失败后再设代理也能恢复（非单例污染）
- [x] 4.2 出片（0.5.47，镜像 `sha-4ddf7f7`）+ 两 CronJob 滚动 + `clusters.image` id9；**另发现并修复了选路链的上游陈旧件**：`proxy-fw` 的 `panel-ops` 镜像没有 `source_provider_routes`/mihomo provider，cheap-1 出片 `fd-proxy-service:sha-f9e677b`（`route_for` 2 处、`providers/mihomo.py`、`NAME=mihomo`）并滚动六节点全 Ready；同时把 35 行 `provider='mihomo-win'` 纠正为注册表用的 `mihomo`（代码里无该字面量，纯数据值），`yfinance → ['mihomo']` 路由行落库后 `/acquire` 首轮即给 mihomo 地址
- [x] 4.3 真管线实测：`instrumented_fetch("yfinance","ticker_history",{"symbol":"AAPL"}, real_source="yfinance")` → **rows=23 / 7.5s**，`fetch_log` 记 `ok`；回归面 `nbs` 2h 内 506 成功/12 失败（直连，无回归）；台账见 §15.138 续十八

## 5. 收尾

- [ ] 5.1 `openspec archive add-yfinance-proxy-lane`
