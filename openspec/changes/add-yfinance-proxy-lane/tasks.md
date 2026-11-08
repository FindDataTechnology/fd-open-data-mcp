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
- [ ] 4.2 出片（0.5.47）+ 两 CronJob 滚动 + `clusters.image`
- [ ] 4.3 台账记一条（含"换 IP 无用、代理+impersonation 才通"的结论与 27/45 数据）

## 5. 收尾

- [ ] 5.1 `openspec archive add-yfinance-proxy-lane`
