# fd-open-data-mcp

[English](README.md) | **中文**

一个**开放数据本体 MCP**：在多数据源的金融/经济数据之上构建语义概念层。你用
**概念 + 实体**来请求数据（例如"茅台的 price.close"、"中国的 GDP"）；系统将概念解析
为各数据源中的物理列，按质量 + 可达性对候选数据源排序，从最佳数据源抓取（带故障
转移），按概念缓存，并按每个概念的频率刷新。

它以**只读**方式消费 finddata 的 `fd-*` 数据源注册表和 `fd-entities-indicators`，
并在其上添加统一层。

## 一键安装

一条自包含的命令块，引导整个 finddata 开放数据栈（枢纽 + 全部数据源包 + 本体数据库）。可重复运行；遇到首个错误即停止。

```bash
# 1) 从 PyPI 安装完整栈。
#    fd-open-data-protocol 被传递引入；fd-polygon 与 fd-cn-report 经 entry-point
#    自动注册。去掉 "[data]" 可轻量安装（仅 MCP 服务器 + CLI，不含
#    akshare/yfinance/playwright SDK）。
pip install "fd-open-data-mcp[data]" fd-polygon fd-cn-report

# 2) 初始化本体数据库并接通每一层：目录 -> 概念 -> 列绑定 -> 每源实体 id
#    -> 刷新计划 -> 清单。
fd-open-data-mcp migrate \
  && fd-open-data-mcp import-catalog \
  && fd-open-data-mcp consume-concepts \
  && fd-open-data-mcp propose-bindings \
  && fd-open-data-mcp seed-entities \
  && fd-open-data-mcp generate-schedules \
  && fd-open-data-mcp register-discovered

# 3) 启动 MCP 服务器（stdio 传输，供任意 MCP 客户端使用）。
fd-open-data-mcp serve
```

实数据抓取需要环境中的数据源密钥（切勿提交）：
`POLYGON_API_KEY`、`EDGAR_IDENTITY`，以及 `fd-cn-report` 所需的 `LLM_*` / `ES_*`。
见各包的配置章节。

## 架构

```
被消费（只读）                       由 fd-open-data-mcp 添加
 fd-akshare/yfinance/world/           concept_bindings     (列 -> 概念)
 cn-report/cn-gov/polygon/            entity_source_identifiers (每源 id)
 datacommons                          source_rankings     (质量 × 可达 × 新鲜度)
 fd-entities-indicators               semantic_observations (读穿透缓存)
   indicator_defs (926 概念)          fetch_log / schedules / executions / policies
   countries/cities/symbols/sw_industries   entities / relationships (图)
        │
   转换器：import_catalog, consume_concepts, propose_bindings,
          seed_entity_identifiers, generate_refresh_schedules
        │
   运行时：read() -> 缓存命中? : 分发（排序 + 故障转移）-> 缓存 -> 记日志
   搜索 ：semantic_search（概念）+ graph_search（实体关系）+ ai_search
```

八大能力（见 `openspec/changes/add-fd-open-data-mcp/specs/`）：
`open-data-catalog`、`semantic-layer`、`entity-identity`、`source-ranking`、
`concept-fetch`、`scheduled-refresh`、`entity-graph`、`vector-search`。

## 安装

```bash
cd fd-open-data-mcp
uv sync                  # 基础安装

# 完整数据源支持（akshare、yfinance、edgar、world bank 等）
uv sync --extra data
```

数据库路径默认为 `fd_open_data_mcp/metadata/daas.db`，可用
`FD_OPEN_DATA_MCP_DATABASE_URL` 覆盖。`FINDDATA_ROOT`（默认：上级 `finddata/` 目录）
用于定位 `fd-*` 提供方。

**注意**：使用 SEC EDGAR 数据前，请在环境中设置
`EDGAR_IDENTITY="your_email@example.com"`。

## 快速开始

```bash
# 1. 建立本体表
fd-open-data-mcp migrate

# 2. 导入目录（akshare 673、yfinance 12、cn-gov 11、cn-report 44、edgar 6……）
fd-open-data-mcp import-catalog
# 或单个提供方：fd-open-data-mcp import-catalog akshare

# 3. 将 926 个 indicator_defs 消费为概念，并提议"列 -> 概念"绑定
fd-open-data-mcp consume-concepts
fd-open-data-mcp propose-bindings

# 4. 为实体播种每源标识符（akshare/yfinance 对应股票，worldbank 对应国家）
fd-open-data-mcp seed-entities

# 5. 从 indicator_defs.frequency 为每个概念生成刷新计划
fd-open-data-mcp generate-schedules

# 6. 按概念 + 实体读取数据（读穿透缓存 + 排序分发 + 故障转移）
fd-open-data-mcp read --concept-id 234 --entity-type stock --entity-id 1 --date 2024-07-26
```

## MCP 服务器

```bash
fd-open-data-mcp serve          # FastMCP，stdio 传输
```

**36 个工具**，跨五个文件注册：

| 分组 | 工具 |
|------|------|
| 目录/绑定 | `import_catalog`、`consume_concepts`、`propose_bindings`、
`list_bindings`、`review_bindings`、`confirm_binding`、`update_binding`、
`list_concepts`、`list_cnreport_rules`、`enumerate_wbgapi_indicators`、
`register_datasource`、`register_discovered` |
| 实体身份 | `seed_entity_identifiers`、`resolve_entity`、`add_entity`、
`update_entity`、`get_entity`、`list_entities`、`add_entity_identifier`、
`ingest_entities_from_dump` |
| 排序/读取 | `rank_sources`、`read`、`fetch` |
| 刷新计划 | `generate_refresh_schedules`、`list_schedules`、`run_schedule`、
`plan_crawl` |
| 实体图 | `add_relationship`、`list_relationships`、`get_entity`、
`graph_search` |
| 向量/语义搜索 | `semantic_search`、`semantic_search_entities`、
`semantic_search_unified`、`ai_search`、`re_embed_concept`、`update_concept` |

## 数据源

分发器 `run_upstream()` 按源名路由到适配器。下表按**真实可达性**而非自报状态排列。

### 已联网验证 ✅

| 数据源 | 说明 |
|--------|------|
| **akshare** | A 股股票/基金/财务数据 |
| **yfinance** | Yahoo Finance 全球股票 |
| **cn-report** | 中国财务报告（44 个工具，见 [fd-cn-report](../fd-cn-report)） |
| **edgar** | SEC EDGAR 备案（需 `EDGAR_IDENTITY` 环境变量） |
| **wbgapi** | 世界银行数据 API |
| **cnstats** | 国家统计局数据 |
| **ckan** | CKAN 目录数据 |
| **nbs-gdp** | 国家统计局 GDP 数据 |
| **datacommons** | Google Data Commons（需 `DC_API_KEY`） |
| **polygon** | Polygon.io 美股（外部包 [fd-polygon](../fd-polygon)） |
| **edinet** | 日本 EDINET 备案 |
| **dartlab** | 韩国 DARTLab 财务披露 |

> `polygon` 与 `datacommons` 的 runner 存在于外部 `fd-*` 包中，通过清单的
> `fetch.module` 懒加载 —— 除非真正发起抓取，否则 `fd-open-data-mcp` 不依赖
> `polygon-api-client` / `requests`。

### 桩适配器 ⚠️（仅返回示例数据，**不**联网）

| 数据源 | 状态 |
|--------|------|
| **cisa-industry** | 桩 —— 返回静态示例行 |
| **amac-fund** | 桩 |
| **shfe-metal-futures** | 桩 |
| **agriculture** | 桩（DCE） |
| **cme-agricultural-futures** | 桩（CME） |
| **chemicals** | 桩 |
| **electronics** | 桩 |
| **nonferrous** | 桩 |
| **flowers-kifc** | 桩 |
| **fin_platforms** | 桩（Wind） |
| **sac-securities** | 桩 |

这些适配器有 `run_<source>()` 入口和清单注册，因此 `list-sources` 自报"✅ Full
support"，但它们返回的是合成/示例数据，而非真实的交易所或协会数据。在依赖其中任何一个
之前，请阅读其适配器文件确认。

### 只读目录

| 数据源 | 状态 |
|--------|------|
| **cn-gov** | 仅清单注册（11 个部委目录） |
| **world** | CKAN + 中文 NBS 统计目录 |

## 爬取控制中心（面板 + 协调器）

策略描述**要爬什么**：概念 × 实体范围 × 日期区间 × 频率 × 模式。`CrawlPolicy` 从面
板创建，由协调器编译为 `CrawlPlan`，再由 `scraw-fd-open-data-mcp` 执行写入
`semantic_observations`。

```bash
# 启动控制面板（默认 http://0.0.0.0:8000）
FD_OPEN_DATA_MCP_DATABASE_URL=<db url> fd-open-data-mcp panel

# 运行一次协调器（到期策略 -> 启动；关闭过期运行）
python -m fd_open_data_mcp.refresh.reconciler
```

**环境变量：**
- `PANEL_TOKEN` —— 若设置，`/panel/*` 需要它（header `X-Panel-Token`、
  `?token=` 或 cookie）。
- `POLICY_MAX_FETCHES`（默认 `50000`）—— 计划大小护栏；抓取估算超过它的到期策略
  会被拒绝（记为失败运行），除非该策略设置了 `force`。
- `RECONCILER_LAUNCHER` —— `scrapyd`（默认）或 `k8s`（`K8sJobLauncher`）。
- `SCRAPYD_URL` / `SCRAW_PLAN_DIR`（scrapyd 启动器），`SCRAW_K8S_NAMESPACE` /
  `SCRAW_K8S_IMAGE` / `SCRAW_K8S_DATABASE_URL` / `SCRAW_K8S_REDIS_URL`
  （k8s 启动器）。
- `FD_PROXY_FORWARDER` —— 本地开发留空（注入层返回直连哨兵 → 直连出口；
  集群抓取由独立 `fd-proxy-service` forwarder 负责代理选择）。旧变量
  `FD_PROXY_POOL`/`FD_EGRESS_MODE` 已不再读取。

## 测试

```bash
uv run --with pytest pytest -q
```

## 设计说明 / v1 限制

- **提议-确认制**："列->概念"绑定携带 `confidence` + `provenance`；低于阈值的绑定不
  参与分发（进入审查队列）。一次真实抓取可将绑定提升为 `sample-confirmed`。
- **排序**按 `(源 × 概念)` 进行，从 `fetch_log` 自调（有界，一次失败不会移除某源）。
- **冲突策略**：每个 `(概念, 实体, 日期)` 一个缓存值，附带 `source_used`；跨源值从不
  合并。
- **LLM 提供方**用于语义富化 / 跨语言概念映射。v1 使用规则表 + `semantic_type` 提示。

详见 `openspec/changes/add-fd-open-data-mcp/` 的完整规范。

## 代理池与断路器

抓取栈使用**代理池**轮换 IP 以避免被数据源封禁，并带 per-(source, real_source×proxy_ip)
断路器。该基础设施部署在远程 k8s 集群的 `scraw` 命名空间。

```bash
# 手动触发一次代理同步
kubectl create job --from=cronjob/proxy-pool-sync proxy-pool-sync-manual -n scraw

# 检查代理池健康
kubectl exec -n scraw fd-open-pg-789d56dbb5-fkdbl -- \
  psql -U postgres -d postgres -c "SELECT status, count(*) FROM proxies GROUP BY status;"
```

关键文件：`fd_open_data_mcp/proxy/`（选择器、断路器、封禁规则、注入）。
`fd_open_data_mcp/fetch/dispatch.py` 中的 real_source 故障转移。

## 标准真实数据源名

- `eastmoney` —— 东方财富（A 股主源，经 akshare）
- `tencent` —— 腾讯财经（A 股故障转移）
- `sina` —— 新浪财经（A 股备选）
- `yahoo_finance` —— Yahoo Finance（全球市场，经 yfinance）

库（`akshare`、`yfinance`）在底层调用多个真实数据源；断路器按真实数据源而非库跟踪
健康，从而支持智能故障转移。

## LLM 配置（用于 PDF 报告提取）

`fd-cn-report` 使用 LLM 从年报 PDF 中提取财务指标。当前 LLM 提供方为
**DeepSeek on Ark**（见 [fd-cn-report](../fd-cn-report) 的 README）：

```bash
# fd-open-data-mcp/.env
LLM_BASE_URL=…/api/plan/v1     # Ark 端点
LLM_API_KEY=…                  # Ark key
LLM_MODEL=deepseek-v4-flash
```

支持 `LLM_API_KEY` 与 `OPENAI_API_KEY`（向后兼容），同时设置时 `LLM_API_KEY` 优先。

## 许可证

MIT
