# 行情源：登记、换源、与 `provisional` 的含义

> 对应 change `add-price-supply-and-caliber`（task 7.3）。这份说明的目标是：**新增一个源、
> 换一个源，全程不改代码**。

## 一句话模型

源不是一个名字，是一组**声明**：它覆盖什么、什么复权口径、锚在哪、能不能对外交付、水位到哪天。
声明在 `fd_open_data_mcp/price_sources/declarations.yaml`；**水位不写在那里**——水位由数据算出来。

```
declarations.yaml（静态：覆盖/口径/许可/访问协议）
        +
库里的观测（动态：水位）
        ↓
list_price_sources()  ← 消费方唯一出口，不缓存水位
```

理由：写死的水位会变成假绿。本项目反复踩过"配置值在启动期被快照"，一个写死的 `as_of_max`
会让消费方以为源还活着。

## 一、新增一个源

在 `declarations.yaml` 的 `sources:` 下加一段：

```yaml
  my-new-source:
    instrument_types: [stock]          # stock | index | fund
    markets: [XSHG, XSHE]
    adjustment: qfq                    # qfq | hfq | none | not_applicable
    adjustment_anchor: "2026-11-10"    # qfq/hfq 必填；或写 per_fetch（见下）
    factor_source: null
    frequency: daily
    license_state: licensed            # self_crawl|licensed|byok|legacy|provisional|seed
    preferred_for: []                  # 只有"首选源"才填（见二）
    access:                            # 取数方式：按协议，不按源名
      kind: federated_view             # federated_view|fd_open_data_ontology|polygon|simulated
      dsn_env: ASTOCK_DSN              # federated_view 必填
    watermark:
      entity_type: stock
      concept_codes: [price.close]
      reference_basket: ["600000", "600036", "600585", "600900", "601318"]
      lookback_days: 400
```

然后跑一次探针确认它被认出来：

```bash
cd fd-open-data-mcp
.venv/bin/python scripts/price_caliber_gate.py --json | head -40
```

**不改代码**。`fd-paper-trading` 的 provider 按 `access.kind` 选实现，源名不进代码。

### 锚点写什么

- 数据是**某一次抓取**算出来的前复权 → 写那次抓取的水位日期（例：`"2026-07-10"`）。
- 每批抓取各自锚定（`akshare` 那种）→ 写 `per_fetch`。这不是"没有锚点"，是一个**已知有害**
  的口径：不同批次之间不可比。实测它与冻结锚点的 `astock_daily` 同日整差 1.00 元（约 2.7%）。
- 指数 → `adjustment: not_applicable`，**不要留空**。空值与"不适用"是两件事。

## 二、换源

换源 = **两件事**，都要显式：

1. 把 `preferred_for` 从旧源移到新源（每个价格族**只能有一个**首选源；两个源同时宣称是歧义，
   加载时直接失败，不是"后来者覆盖"）。
2. 旧源要么保留（作对照，`preferred_for: []`），要么删除声明。**保留行**——历史观测不删，
   源换过之后它们仍要能被读出来作对比。

换完必须做的一件事：**记下锚点变化**。因为换锚点会让历史回测结果跳变，而两次结果长得一样。
`PriceSeriesRef.fingerprint` 会变，受影响区间标"不可比"，不要拿新旧两组数字直接比。

## 三、`provisional` / `seed` 是什么意思

| license_state | 能内部消费 | 能对外交付 | 说明 |
|---|---|---|---|
| `licensed` / `byok` / `self_crawl` | ✅ | ✅ | 正常可交付 |
| `provisional` / `seed` | ✅ | ❌ 需附声明 | 临时源，待持牌源替换 |
| `legacy` | ✅ | ❌ | 遗留表，出处口径未定 |

对外交付引用 `provisional`/`seed` 源时，**必须**带来源与"待替换"声明，缺了直接拒绝
（`price_sources.delivery.assert_delivery_allowed`）。警告会被忽略，"默认可用"正是要消灭的。

`legacy`（当前 `astock_daily`、`akshare` 都属于此）默认**不可对外交付**。这是刻意保守：这两张表
出处口径未经复核。

**但交付资格可以在声明里覆盖**，不必改代码——因为"能不能给客户看"是执照与合同问题，
不是数据出处问题：

```yaml
  astock_daily:
    license_state: legacy        # 说"数据从哪来"
    delivery: deliverable        # 说"我们有权给客户看吗"（auto | deliverable | internal_only）
```

`delivery: auto`（默认）按 `license_state` 推演；`deliverable` / `internal_only` 是显式覆盖，
两个方向都能拨。**改之前先确认抓取主体与授权**——这个开关只是让决定落在一行 YAML 上，
不代表它已经合规。

## 四、指数（基准）从哪来

指数族（`price.index.daily`）实测**全库零观测**，而价格抓取自 2026-10-07 冻结，不能靠重抓补。
所以走一次性种子通道：

```bash
# CSV: date,symbol,open,high,low,close,volume,amount,name
.venv/bin/python scripts/import_index_seed.py seed.csv --dry-run   # 先看会写什么
.venv/bin/python scripts/import_index_seed.py seed.csv
```

- 落 `source_used='index-seed'`、`license_state=provisional`（对外须声明）。
- 幂等：重跑同一份 CSV 不会产生重复行。
- 空值**不落行**（"没有"不是"零"）；坏行进 rejected 并给原因，其余照写。
- 指数与个股同码（`000300` 既是沪深300也是厦门港务）按 `entity_type` 区分，永不按裸码解析。

持牌源到位后：登记新源 → 把 `preferred_for: [price.index.daily]` 移过去 → `index-seed` 退为
对照源。**不需要删数据**。

## 五、常见误判

| 现象 | 不是 | 是 |
|---|---|---|
| 水位停在几个月前 | 探针坏了 | 源停更（价格抓取 2026-10-07 冻结），是真结论 |
| 三路水位不一致 | 某一处读错了 | 三路真的指向不同源；探针会点名三条路径 |
| 指数指标全是 null | 代码没实现 | 输入为空；指数族零观测 |
| 两个源同日差 1 元 | 数据脏 | 复权锚点不同；按各自锚点分别出数，不混读 |
| `dry_run` 报"概念族不完整" | 库坏了 | 概念族确实缺；先建族再导 |

**三种"像水位"的东西，粒度不同，不要互相替代**：

| 名字 | 粒度 | 用途 |
|---|---|---|
| `as_of_max`（本注册表） | **源级**：这个 source 到哪天有数据 | 陈旧度判定、三路同源断言 |
| `StoreCensus.time_range_end` | **存储级**：这个库/分片到哪天 | 存储普查页，与"哪个源"无关 |
| `strategy_runs.params.last_bar_date` | **逐 run**：这个策略处理到哪根 bar | 防同一根 bar 被重复处理 |

把三者混为一谈会得出两种相反的错结论：拿 run 游标当源水位 → 一个从没跑过的策略显示"源没数据"；
拿源水位当 run 游标 → 同一根 bar 被重复下单。

## 六、跑闸

```bash
# 三条判据，只读，写零文件。退出码 0=通过 1=有违规 2=跑不起来
.venv/bin/python scripts/price_caliber_gate.py
.venv/bin/python scripts/price_caliber_gate.py --json
```

`2` **不是通过**：探针自身故障与"检查通过"必须可区分。

> 本机注意：`FD_OPEN_DATA_MCP_DATABASE_URL` 会被 `db/__init__.py` 里的
> `load_dotenv(".env.local", override=True)` 覆盖。用别的 DSN 试跑时，请在一个**没有
> `.env.local` 的目录**里执行（dotenv 路径是相对的），不要改仓库里的 dotenv 策略。
