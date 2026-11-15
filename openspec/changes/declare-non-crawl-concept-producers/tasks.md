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

- [x] 4.1 **8 条宏观**（6/88/89/90/91/92/227/232）：在提案里记明它需要三件——
  取回 `feat/datacommons-datasource` 的 `bulk_ingest_country_macro.py`、在
  `fd-ashare` 注册 3 个函数 + 补 `非制造业-指数` 列、补 `concept_bindings`。
  验证：这三件在 tasks 里各留一条「未做」并以 `blocked:` 标注外部依赖。
  **已按验证条件记下三件（2026-11-15 复核，其中两件的口径被本次测量订正）**：
  这 8 条码是 `PMI_NONMFG`(6) / `FAI_CUMULATIVE`(88) / `RETAIL_MONTHLY`(89) /
  `RETAIL_YOY`(90) / `GDP_AMOUNT`(91) / `GDP_YOY`(92) / `FAI_MONTHLY`(227) /
  `RETAIL_CUMULATIVE`(232)，声明文件里 8 条全是 `undecided`、基线里全在 `dormant`。

  1. **ETL 脚本落地** —— 未做，`blocked:` 只需裁决，**不需要外部仓**。
     `bulk_ingest_country_macro.py` 只在 `feat/datacommons-datasource`（tip
     `e427487`，相对 gitee/main 只多 2 笔提交，但分叉点后 main 已前进 **419** 笔，
     整分支 38 文件 / 8601 行）。**但脚本本身 320 行，`import` 只有 stdlib +
     `sqlalchemy` + `psycopg2.extras`，不 import 本包任何模块** ⇒ 可以**只搬这一个
     脚本**当 CronJob 跑（它自带 `--db-url`/`--dry-run`，自己 `execute_values` 上写），
     不必合那 38 文件。这条口径与提案初稿「先把那条 ETL 合进来」不同，代价小一个量级。
  2. **`fd-akshare` 注册 3 个函数 + `macro_china_pmi` 补 `非制造业-指数` 列** ——
     未做，`blocked:` 外部仓**不在本工作区**。提案记的是
     `macro_china_gdp`/`macro_china_gdzctz`/`macro_china_consumer_goods_retail`
     未登记、`macro_china_pmi`(fn 1152) 只登记了「制造业-指数」。**本次判不出**：
     `catalog/providers.py` 的 `_p("fd-akshare", …)` 找的是兄弟仓
     `fd-akshare/fd_akshare/metadata/registry.db`，本工作区没有该仓、也没有那份
     registry.db。所以这三条只能照提案记，**不许当成已核**。
  3. **`concept_bindings`** —— 未做，`blocked:` 要先裁决「绑函数」还是「靠 ETL 养」。
     实测：8 条的 `concept_bindings` **0** 行、`source_rankings` **0** 行；
     数据在**分片 `shard_xinru2`**（`shard_xinru3` 为 0），末周期
     `2026-06-01`～`2026-07-01`（PMI 到 `2026-07-01`、GDP 到 `2026-06-30`），
     最后一次 `fetched_at` **2026-08-17**（≈90 天前、且 8 条同一时刻 ⇒ 是**一次手工
     回填**，不是定时作业）。**踩到一个会误判的坑**：只查主库会得到「8 条一行观测都
     没有」，进而读成「从未有数据」；真相是分片里有行——判这批必须查分片。
     两条路互斥：绑函数＝让爬取规划去 fetch；靠 ETL＝照 batch1/batch2 那样挂 CronJob。
     选哪条是产品裁决（与「重新点火 or `deprecated`」是同一个决定）。
  *（本 change 到这为止：判据与待办已出齐。**剩下的是裁决**：这 8 条重新点火
  （按第 1 条只搬脚本即可起步）还是标 `deprecated`。）*
- [x] 4.2 **农业族 79 条**（`extract_batch2.py` 那类 additive 回填）：写清「复跑插 0 行」
  意味着**它不是增量刷新**，要判它是否够格当 `bulk_etl` 产出者。
  验证：结论落进 `evidence`，并在提案的 Impact 里点名。
  **结论：够格**（2026-11-15 实测，方向与任务原本的担心相反）。三条实测：
  ① 66 条 batch2 概念**全部**库内有行，`source_used` 与 `_SOURCE_OF` 逐条一致；
  ② 同一抽取器 **9 条在 0.5 天内插过新行**（三条 `agri.*` 在 `2026-11-14T18:03Z`
  那一轮），**57 条整齐停在 8.8 天**＝那几个源上一次发新周期的时间，不是停了；
  ③ 覆盖补齐：三条 `week` 粒度 27 行覆盖 `2026-05-04→2026-11-02` 无缺口，
  `agri.cereal.utilization.amis` 是 `year` 粒度、54 行已到 **2026**。
  所以「复跑插 0 行」是 additive 契约（`ON CONFLICT DO NOTHING`、`fetched_at=now()`
  只在真插行时写）的正常形态；**判活得看抽取器整体**，逐概念拿 `fetched_at` 判会把
  年度/月度序列判成停摆（1.2 已排除过同一个坑）。66 条 `evidence` 已追加这段，
  提案 Impact 新增「additive 回填够不够格当产出者」一节。
  **预告一条**：`amis` 那类年度序列未来约一年不会有新行，声明会在 120 天后
  （约 2027-03-15）按 `STALE_DAYS` 自动降级为 `dormant`——预期提醒，不是回归。
