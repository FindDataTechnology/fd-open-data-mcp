## Why

2026-11-15 实测（本体 `fd_open_data` + 两个分片，只读）：**在用概念 1024 个，
零 `concept_bindings` 的 750 个，其中 501 个 `semantic_observations` 里已经有行**。

「有数据 + 零绑定」在监控上是**静默**的：规划器（`crawl/planner.py`）为每个概念
拼 `ranked_sources` 要求 confirmed binding，没绑定就直接进 `plan.unroutable`
（reason: `no confirmed binding / no candidate source`），于是**任何策略、任何
crawl 都不会再取它的新值**；而库里的旧行照常被读出来，新鲜度闸只会说「过期」，
不会说「爬虫为什么不会再取它」。

这条线原本连判据都没有——`fd-infra-deploy/tools/check_concept_routability.py`
（本 change 之前新建，`b95dd5c`）现在能把它们点出来，但它只覆盖**一条路径**。
把 501 条按「仓里有没有产出者」拆开之后，才发现问题不是一个：

| 分档 | 数量 | 含义 |
|---|---|---|
| 写侧脚本里有该概念码 | **97** | `extract_*` / `bulk_ingest_*` / `seed_*` / `sync_*` / `update_*` 类脚本正文里出现，多半是批量 ETL 的产出者 |
| 只被闸/测试/夹具/清单引用 | **97** | 概念被某处**声明**过，但这批脚本里找不到写它的地方 |
| 扫过的树里连字面量都没有 | **307** | 仓内找不到任何产出者痕迹 |

（口径：`fd-infra-deploy/tools/gen_concept_producers.py` 扫 2 棵写侧树
`fd-industry-data/scripts` + `fd-open-data-mcp/scripts` 与 3 棵参考树
`fd-infra-deploy/tools` + `fd-open-data-mcp/tests` + `fd-industry-data/tests`，
共 **534** 个 `.py`；只做**字面量出现**判定——所以 97 是**上限**、307 是**下限**，
两者都不是「证明了有/没有产出者」。范围与文件数落在声明文件的 `split.scope` 里，
闸把三档与上下限**同屏**打印。）

> **别把两组数混用。** 本提案初稿的 98 / 102 / 301 来自一次性 ad-hoc 扫描，
> **扫描范围没记录**（`--refs` 那三棵参考树当时根本没定义）。落进仓里的生成器是
> 唯一可复现的口径，**以 97 / 97 / 307 为准**；差异只两条来源：每行只取**首个**
> 字面量（98→97）、参考树集合不同（102/301 → 97/307）。

另一条被证伪的假设：`registry_entries` **不是**产出者声明面。零绑定的 630 个
概念在注册表里有 `semantic_code` 命中，但其中 **629 个的 `source_table` 是
`concepts` 本身**（`source_db='fd_open_data'`、`source_column='code'`），剩下 1 个
指向 `semantic_observations`——**两个都是本库内部表**，没有一条指向上游。
换句话说：库里 5.8 万行注册表映射的是「源表某列 → 语义码」，对这批概念，被映射
的「源」就是概念目录自己。把这 630 读成「630 个已有声明」会把自指当成出处。

一条具体的、值得当样本的：concept 6/88/89/90/91/92/227/232（非制造业 PMI、
固投累计/当月、社零当月/累计/同比、GDP 当季/同比）最后一行停在
`2026-06-01`～`2026-07-01`，`source_rankings` / `concept_bindings` /
`concept_source_stats` **三表全空**；唯一把它们映射到 akshare 函数的代码
（`CN_MACRO_MAP`，在 `scripts/bulk_ingest_country_macro.py`）**只活在分支
`feat/datacommons-datasource`**（`2294189`，2026-08-16，从未合进 main）。而且
akshare 侧也不全：`macro_china_gdp` / `macro_china_gdzctz` /
`macro_china_consumer_goods_retail` 在函数目录里**根本没登记**，
`macro_china_pmi`（fn 1152）只登记了「制造业-指数」一列、缺「非制造业-指数」；
函数目录来自**另一个仓** `fd-akshare` 的 `metadata/registry.db`（`catalog/providers.py`
的 `_p("fd-akshare", …)`），本工作区没有那个仓。

所以「补个定时任务」修不了这批：得先有绑定（或先把那条 ETL 合进来），而
「哪些概念该绑、哪些该退役、哪些本来就靠 ETL 养」**目前没有任何地方可写**。

## What Changes

- **加一处「非爬取产出者」声明面**：`tools/concept-producers.json`（随闸入库、
  review 可见），逐概念写明 `kind`（`crawl` / `bulk_etl` / `manual_import` /
  `retired`）、`producer`（脚本路径或策略名）、`schedule`（有排期就写，没有写
  `manual`）、`evidence`（为什么这么判，一行）。**不新造数据库表**：产出者是
  代码，代码在 git 里；表会与代码漂移，而这份文件与闸同笔 review。
- **闸读它并分四档说话**：`routable`（有绑定）/ `etl_fed`（声明为 bulk_etl 或
  manual_import，**不计命中**，但打印「它的产出链是 X、排期是 Y」）/
  `dormant`（无绑定、未声明、有数据 ⇒ 命中）/ `never_served`（无绑定、无数据、
  未声明 ⇒ 事实）。**未登记的新概念仍会自己冒出来**（这条是枚举型闸的命门，
  不能因为加了声明面就要求先登记）。
- **声明必须能过期**：每条 `bulk_etl` / `manual_import` 声明都要写
  `last_verified`（日期）。超过 `STALE_DAYS`（默认 120 天）未复核 ⇒ 降级为
  `dormant` 并点名「声明过期，需复核」——否则这份文件会变成一张永远为真的
  豁免表（同族事故见 `check_local_path_volume_backups.py` 的 `RESCUE_PVC`
  必须带到期日）。
- **`retired` 走 `deprecated`，不走本文件**：真想退役就改 `concepts.deprecated`，
  本文件只描述「谁在产」，不承担「这个概念不要了」的语义——两件事写在一处会
  让人分不清「没有生产者」与「已决定不要」。
- **把三档拆分写进 proposals 与闸的输出**：97 / 97 / 307 是**上限/下限**，
  必须随数字一起打印口径，避免下一个人把 307 读成「307 个永远没人产」。
  已落地：`gen_concept_producers.py` 扫出三档写进 `split` 段，闸用
  `format_split()` 把数字与口径打成相邻两行（拆开打会被读成结论）。
- **收窄人口不许污染回归口径**：`--category` 只收窄清单，回归与命中仍按全量判，
  且 `--write-baseline` 与 `--category` 同时给直接拒（快照是全量口径，按一族
  重写会把它静默砍掉）。实测过旧口径在同一份数据上打 `已恢复 491 条`。

## Impact

- Affected specs: `catalog-enumeration`（概念目录与产出者声明的边界）
- Affected code:
  - `fd-infra-deploy/tools/check_concept_routability.py`（读声明、四档、过期降级）
  - `fd-infra-deploy/tools/data/concept-producers.json`（新增，初始覆盖 97 条写侧命中 + 其余标 `undecided`，全部 `last_verified` 空 ⇒ 判定不变）
  - `fd-infra-deploy/tools/gen_concept_producers.py`（新增，生成骨架 + 算三档拆分，`--check` 可回归）
  - `fd-infra-deploy/tools/data/concept-routability-baseline.json`（重算，`dormant` 会因声明而变小）
- 不改数据库 schema、不写生产库；`concept_bindings` 的写入留给各自的「补绑定」
  change（宏观那条另算，见 Why 末段）。

### additive 回填够不够格当产出者（农业族那一类，2026-11-15 定）

`extract_batch2.py` 那类写入是 additive 的（`INSERT … ON CONFLICT ON CONSTRAINT
uq_sem_obs DO NOTHING`，`fetched_at = now()` 只在真的插进一行时才有），所以
**复跑插 0 行是常态**。这一点曾被当成「它不是增量刷新 ⇒ 不该算产出者」的理由，
实测下来方向反了：

- 66 条 batch2 概念**全部**在库里有行，`source_used` 与 `_SOURCE_OF` 逐条一致；
- 同一个抽取器 2026-11-15 实测 **9 条在 0.5 天内插过新行**（`agri.hog.price.spot`
  ／`agri.pork.price.market`／`agri.pork.price.wholesale` 三条在
  `2026-11-14T18:03Z` 那一轮），**57 条整齐停在 8.8 天**——那是那几个源上一次
  发新周期的时间，不是它们停了；
- 4 条农业概念的覆盖也补齐了：三条 `week` 粒度 27 行覆盖
  `2026-05-04→2026-11-02` 无缺口；`agri.cereal.utilization.amis` 是 `year`
  粒度、54 行已到 **2026**（年度序列，下一个可用周期是 2027）。

结论：**够格**。判活的口径必须是「抽取器整体有没有在产」，不是「逐概念
`fetched_at` 新不新」——后者会把年度/月度序列的正常周期判成停摆（同一个坑在
1.2 里已经排除过：batch2 的 66 条里 57 条落在 8–60 天，而它们全是健康的）。

连带一条要预告的：`agri.cereal.utilization.amis` 这类年度序列未来约一年不会有
新行，它的声明会在 120 天后（约 2027-03-15）按 `STALE_DAYS` 自动降级为
`dormant`。**那是预期的复核提醒，不是回归**——这条写进 evidence，免得届时被
当成闸坏了。
