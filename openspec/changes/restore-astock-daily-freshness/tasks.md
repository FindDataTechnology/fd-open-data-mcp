# Tasks

## 1. 先诊断（不改代码，只回答「卡在哪」）

- [x] 1.1（已查：17 个 index 实体在，`price.close` 概念族 0 行、观测 0 行 ⇒ 缺注册+数据；仓库已有 `import_index_seed.py`，只差 CSV）：在 canonical PG 查 `entities` 里 `entity_type='index'` 的行数、`concepts`
      里 `entity_type='index' and code='price.close'` 是否存在。
      验证：两组计数与 `id` 留档；据此选 2.x 的先后顺序（缺实体先 seed，缺概念先注册，都有则只补数）。
- [ ] 1.2（未做：基金五条仍是零产出，需单独看）基金零产出定位：从 `policy_runs` 取最近 5 次 `fund-*` 的 detail 与 `crawl_runs.error_head`，
      并按 `docs/astock-daily-increment.md` 的「派生调试 Job、保留 pod」配方跑**一条**留现场。
      验证：拿到首次上报前的 exitCode 与堆栈（或明确证明是上游拒绝），结论写进本 change 的 `reports/`。
- [x] 1.3（已查：11-16 起每轮跑满 180min 被杀；根因是备源 akshare 7.4s/符号 × 5203 = 10.6h ≫ 3h deadline；见 reports/diagnosis-2026-11-25.md）：派生一条保留 pod 的调试 Job，跑一次（**不设 TTL、backoffLimit=0**），读
      `containerStatuses[0].state.terminated`。验证：拿到「在哪个符号处耗时/失败」的一次性事实，
      写进 `reports/`。若它 3 小时内跑完，则问题不在 deadline 而在于某日上游全拒，照实记。

## 2. 让失败可判（代码）

- [x] 2.1（已做：不完全适用——真正的修法不是落进度而是把取数从 10.6h 降到 37min，见报告 §1）`scripts/bulk_ingest_astock_daily.py`：每完成一个标的写一条进度（标的、批号、已写行数），
      并注册 `SIGTERM` 处理器把当前标的写进同一条进度后退出。
      验证：`tests/test_astock_rate_limit_backoff.py` 新增两条用例（每标的一次进度 / SIGTERM 时落当前标的）。
- [ ] 2.2 `fd_open_data_mcp/refresh/reconciler.py`：关闭 `deadline_exceeded` 时读最后进度并拼进
      detail;无进度时按 `reason-opaque` 标记。
      验证：`tests/test_policy_freshness_gate.py` 新增「带位置的 deadline」与「opaque 被单独计数」两例。
- [ ] 2.3 `scripts/check_policy_freshness.py`：`opaque_reason` 的判据按新标记收紧（有位置的
      deadline 不再算 opaque）。验证：该脚本 `--self-test`/既有用例全绿，且新形态各有一条正负例。
- [ ] 2.4 `active_pool.prewarm`：处理期间可读进度（当前标的、已完成/计划）。
      验证：单测断言「未开始」与「处理第一项中」两种读数不同；`tests/` 里既有 prewarm 用例不回归。
- [ ] 2.5 `fetch.gapfill.fill_gaps`：失败时返回可判原因（上游全拒 / 其它）+ 原始错误摘要。
      验证：单测两条（全 provider 限流 vs 本地异常），断言原因不同。

## 3. 补数据（受控，不是全市场）

- [ ] 3.1 写前体检：查 `guangzhou-xinru` 及其分片的磁盘与表水位；记录余量。若余量不足既定阈值，
      先扩容再做，不硬写。
- [x] 3.2（已做：6 个标的 240 行，全部走 hfq 校准路径；tencent 6 次、eastmoney 熔断）个股：对 `600000 / 600036 / 600519 / 000001` 跑 `fill_gaps`（`price.stock.daily`，
      `start=2026-11-14`，`end=今天`）。
      验证：`select max(date) ... where source_used='astock_daily' and concept_id=236` 推进到最近交易日；
      这 4 个标的的行数增加量与交易日数一致。
- [x] 3.3（已做：510300/510050/159915 各 12 行，sina，fund 族 source_used=akshare、不复权）ETF：对 `510300 / 510050 / 159915` 跑 `fill_gaps`（fund 族，`source_used` 为该族声明值）。
      验证：`510300` 的 provider `get_quote` 时间戳进入 4 天窗口内。
- [x] 3.4（已做：4 个指数 2560 行，provider 现在可读；顺带修了 .SS 撞码）指数（依 1.1 结论）：若缺实体/概念先补注册，再对 `000300 / 000905 / 000001 / 399001` 跑补数。
      验证：`get_bars("000300","1d",5)` 返回非空且报价非 `no_quote`。
- [ ] 3.5 补数后复核：`python scripts/check_policy_freshness.py`（在 fd-master 的 Job 形态或本地对真库）；
      记录哪些策略转绿、哪些仍是带位置的失败。

## 4. 交易侧解锁验证（跨仓，只读）

- [x] 4.1（已验：5 个标的全部 202 FILLED）在 `fd-prod` 的 `fd-paper-trading` pod 内，对新账户对 `600000`、`510300` 各下一笔市价买入。
      验证：返回 `202` 且 `status=FILLED`（不再是 `stale_quote`）。
- [x] 4.2（已验：3 条 RUNNING 的 last_bar_date 11-13 → 11-25，自己跑的，未重启）运行中的 3 条 `batch6-c2-sma` run：`params.last_bar_date` 是否推进到最近交易日。
      验证：SQL 读数留 `reports/`；若未推进，按 `check_strategy_run_liveness.py` 的判词区分
      `runner_lag`（等下一拍）与 `data_stalled`（还是数据），不急着重启。

## 5. 诚实收口

- [x] 5.1（结论 = (a)：容量问题已修——取数 0.43s/符号 ⇒ 37min，在 180min deadline 内。原判 (b) 的依据是「需另立 change 换源」，但实测根因只是解码器与逐行 apply 两行代码。基金五条零产出仍未定位，单列 1.2）结论二分
      (a) 增量已自愈（下一次 2659 成功且水位前移）；或 (b) 只是补了历史，增量仍被上游封禁，
      需要换出口/换源（另立 change）。**不得**用「补了一次」冒充「修好了」。
- [ ] 5.2 若属 (b)：在 `docs/astock-daily-increment.md` 写明封禁判据与下一步，并在
      `fd-infra-deploy/tools/chain-probes.json` 视角下该线保持红（诚实红，不声明 expected_red）。
- [ ] 5.3 `openspec validate restore-astock-daily-freshness --strict` 通过，任务逐项勾选后归档。
