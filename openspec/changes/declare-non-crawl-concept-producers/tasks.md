## 1. 声明面（数据文件）

- [x] 1.1 生成 `fd-infra-deploy/tools/data/concept-producers.json` 的**骨架**：
  从基线里 501 条 `dormant` 出发，把写侧脚本命中的写成候选条目
  （`kind: bulk_etl`、`producer` 填命中的脚本路径、`schedule: unknown`、
  `last_verified` 留空 ⇒ 视为未复核），其余写 `kind: undecided`。
  验证：`len(d['producers'])` = **501**；生成器实跑 **97** 条 `bulk_etl` +
  **404** 条 `undecided`（提案初稿写 98，差 1 是因为生成器每行只取首个字面量；
  以仓内生成器为准）。**全部 `last_verified` 为空 ⇒ 判定一条不变**（501 仍是 501）。
- [x] 1.2 逐族复核并填 `schedule` / `last_verified` / `evidence`：先做 5 个最大的族
  （行业经营 113、农业 79、电力与碳 59、利率 29、城乡建设 29）。
  **复核判据**：脚本里真有写路径（INSERT/upsert 到 `semantic_observations`），
  不是只在注释或告警清单里出现。验证：每条 `evidence` 写出文件名 + 行号。
  落地：**97 条全部复核**，三条互相独立的证据同时对得上——
  ① 代码级出现 97/97（按 `#` 前缀区分，没有一条只活在注释里）；
  ② 写入方自己的查表能解析 97/97（`_SOURCE_OF[code]` / `CONCEPT_ENTITY` /
  `INDICATOR_MAP+YJYG+YJKB`；解析不到会在 insert 时 `KeyError`，batch2 的
  55+11=**66** 与申报的 66 逐条相等）；③ 库内 `source_used` 与那张表逐个一致
  **97/97**（行为侧：行确实是这份代码写的）。`evidence` 逐条点名
  概念表行 + 共享写入行（batch2 `:808`／batch1 `:430`／pharma `:146`／stock `:146`）。
  `SKIP_NOTES` 那 4 条 `pwr.grid.*` 按真实声明位置写（`:329-332` 的 EIA 系列码映射
  与 `_SOURCE_OF:462-465`=eia-v2），没把「顺带提到」当声明。
  **顺手排除一条假判据**：`fetched_at` 新鲜度不能用来判死活——batch2 的 66 条里
  57 条落在 8–60 天，而 additive 契约本来就只在源发布新周期时插行。
- [x] 1.3 `schedule` 必须写**实际排期**：CronJob 看 `spec.schedule`、宿主 cron 看
  crontab、都没有写 `manual`。验证：任取 5 条，`kubectl get cronjob` / `crontab -l`
  能对上；对不上就是抄错了。
  **四条全部线上读到**（不是从清单猜）：`fd-prod/semantic-extract-batch 35 5 * * *`
  （batch1/2，Job Complete 用时 5m16s）、`tencent/mcp/fd-pharma-observations
  30 5 * * * Asia/Shanghai`（跑 `/src/extract_pharma.py --apply`，最近三轮全 Complete）、
  `crawl_policies` **id 2658** `43 20 * * 1-5` UTC（enabled，脚本
  `bulk_ingest_stock_indicators_events`，last_run 11-13T20:52Z）。

## 2. 闸读声明（四档）

- [x] 2.1 `check_concept_routability.py` 新增 `--producers <file>`（缺省取仓内
  `tools/data/concept-producers.json`，读不到 ⇒ **退 2**，不许静默当「没有声明」）。
  验证：`--producers /nonexistent` ⇒ EXIT=2 且 stderr 说明读不到。**实测通过。**
  缺省路径相对**本文件**（pod 里 CWD 是 `/`），`__file__` 取不到时退 `tools/data/...`。
- [x] 2.2 `classify()` 从三档扩到四档：`etl_fed` 仅在声明存在且未过期时成立；
  `etl_fed` **不计命中**但必须打印 `producer` 与 `schedule`。
  验证：夹具里一条 `kind: bulk_etl` + `last_verified: today` ⇒ 判 `etl_fed`、
  退出码不受影响。**真库实测**：探针声明 `agri.cereal.utilization.amis` ⇒
  `非爬取产出 1 / 停摆 500`，基线档报 `已恢复 1` 并点名。
- [x] 2.3 过期降级：`last_verified` 超过 `STALE_DAYS`（默认 120，可用
  `--stale-days` 覆盖）⇒ 回 `dormant` 并单列一行「声明过期，需复核」。
  验证：夹具 `last_verified: 2026-01-01` ⇒ 该条判 `dormant` 且打印过期原因。
  `kind=undecided` 与**缺 `last_verified`** 同样按「无声明」处理。
- [x] 2.4 `--self-test` 补全：`etl_fed` 生效／过期降级／`undecided` 仍算
  `dormant`／声明文件读不到 ⇒ 退 2。验证：`--self-test` 全绿且条数 **8 → 14**
  （后又补到 **20**，见 3.1）。

## 3. 输出口径（上限/下限必须同屏）

- [x] 3.1 打印三档拆分（写侧命中 / 仅被闸·测试·夹具引用 / 无字面量）**并与分档数
  同屏**，且同屏写明「第一档是上限、第三档是下限」——口径与数字分两处打，读者只会
  记住数字。验证：实跑输出里三档数字 + 上下限说明同时出现。
  落地：生成器算拆分写进声明文件 `split` 段（含 `scope`：扫过的树与 `.py` 数），
  闸 `format_split()` 照印两行。**实测**：`写侧命中 97（上限） / 只被闸·测试·夹具命名
  97 / 无字面量 307（下限）`，扫过 534 个 `.py`、5 棵树；`split` 段缺失或计数不全
  ⇒ 不出这几行（不编造）。顺带修掉一处会撒谎的读数：`--category` + `--baseline`
  旧口径在同一份数据上打 `已恢复 491 条`（实际 0），现在回归/命中恒按全量判、
  `--write-baseline` 与 `--category` 同用直接退 2。
- [x] 3.2 基线重算：声明生效后 `dormant` 会少于 501，用 `--write-baseline` 重种并在
  提交信息里逐条说明 delta（哪一族因哪份声明而退出）。验证：重种后 `--baseline`
  实跑 `新增 0 / 已恢复 N`，`N` 与提交信息里的条目数一致。
  落地：**501 → 404**；重种前实跑 `已恢复 97 条`，重种后 `新增 0 / 已恢复 0
  （快照内共 404 条）`，EXIT=0。差集逐条核过（少了 97、多了 0）。
  退出的 97 条按族：城乡建设 29、能源 11、价格指数 8、未分类 6、货币与社融 5、
  农业 4、电力与碳 4、灾害 3、快递物流 3、营运能力 3、盈利能力 3、工业 2、
  偿债能力 2、成长能力 2、利率 2、业绩快报 2、业绩预告 2、汽车 1、其他 1、
  每股指标 1、价格 1、科研 1、交通 1。
  **顺带修掉一个自伤**：重种之后生成器的人口只取 `baseline.dormant`，
  那 97 条已不在其中 ⇒ 再生成一次会把复核**整条删掉**（实测输出
  `写侧命中 0`）。现在人口 = 快照 ∪ 已复核条目（未复核且不在快照里的照旧丢），
  生成器自证 7 → 9 条、红证可复现。

## 4. 两个具体族的处置（不在本 change 内改库，只出判据与待办）

- [ ] 4.1 **8 条宏观**（6/88/89/90/91/92/227/232）：在提案里记明它需要三件——
  取回 `feat/datacommons-datasource` 的 `bulk_ingest_country_macro.py`、在
  `fd-ashare` 注册 3 个函数 + 补 `非制造业-指数` 列、补 `concept_bindings`。
  验证：这三件在 tasks 里各留一条「未做」并以 `blocked:` 标注外部依赖。
  *（`blocked:` 外部仓 `fd-ashare` + 未合分支；等裁决：重新点火或 `deprecated`。）*
- [ ] 4.2 **农业族 79 条**（`extract_batch2.py` 那类 additive 回填）：写清「复跑插 0 行」
  意味着**它不是增量刷新**，要判它是否够格当 `bulk_etl` 产出者。
  验证：结论落进 `evidence`，并在提案的 Impact 里点名。
  *（1.2 的复核已给出事实基础：这 4 条 `agri.*` 的 `source_used` 与 `_SOURCE_OF`
  逐个一致、`fetched_at` 8–60 天，属「源没发新周期」而非「停了」；够不够格仍待裁决。）*
