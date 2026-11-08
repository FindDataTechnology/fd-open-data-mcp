# Changelog

All notable changes to this project will be documented in this file.

## [0.5.52] - 2026-11-08
- **fix(reads)**: 0.5.50 那刀钉错了对象。它约束的是 `semantic_observations.entity_type`
  ——**行自己声明的类型**——而错位行声明的恰好也是 `'stock'`，所以拿行业 id / 基金经理 id
  去查股票概念照样取得到。真库实测 2026-11-08（715.9 万行）：声明 `stock` 但实体不是股票的
  观测共 **343,411** 条 —— 基金(经理 `person`) **333,975**（865 个实体，每人约 443 行
  `price.high/low/open/close/amount`，2026-06-09~09-18，源 akshare，标识是天天基金经理 id）、
  行业 8,500、基金(fund) 936。现以**实体自身的类**为准：
  - 新增 `entities.resolver.check_entity_identity`（证券类 `SECURITY_ENTITY_TYPES={'stock'}`），
    在三处缓存读入口 `read_cache` / `read_cache_all` / `read_cache_range` 校验
    （`read` / `fetch` / `read_series` / gapfill 全部经由此处），错误信息说明"这是错挂数据、
    不是覆盖缺口"；实体行缺失不拦（悬空 id 属另一类缺陷）。
  - `hotel_tools.hotel_series` 改为**要求行声明的类等于其所属实体的类**
    （`EXISTS (... e.entity_type = o.entity_type)`），证券概念再加实体必须是 stock；
    `hotel_kpi` 的 RevPAR/ADR/occupancy 分支同样加实体为 stock 的条件。
  - `analysis/backend.time_series` 对证券类加同样的 `exists()` 过滤。
- **fix(hotel_series) 回归**: 0.5.50 的 `o.entity_type = 概念类型` 误伤了合法跨层——星级季报
  同一概念按城市/省/国家三层存（城市行 5,565 条挂 city 实体，概念却是 `region` 型），
  按概念类型钉行会让城市级读数为 0。真库实测：修复后
  `hotel.cn.star_rated.count.all.quarterly` + 北京(BJS, id=17) 回到 5 点
  （2023-06-30~2024-09-30）；行业/基金经理 id 仍为 unavailable。
  为什么不能一刀切"实体类型 == 概念类型"：`country→city` 6,302、`region→city` 5,565、
  `region→country` 96 是产品的多层级形态，硬相等会读没真序列。
- 已证写入侧无新增污染：`fetched_at > 2026-11-06` 的错位行为 0 条（写侧 `3225f4d` 之后）。

## [0.5.51] - 2026-11-08
- **fix(astock)**: `bulk_ingest_astock_daily.py` 补 `import os`。0.5.48 把 DSN 从命令行挪进
  环境变量时写了 `db_url = args.db_url or os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL")`
  却没导入 `os`，于是**每一次** direct-script 派发都在这一行
  `NameError: name 'os' is not defined` 退出 1 —— 约 5 秒就死，赶在 runner 报到之前，
  所以 `policy_runs` 只能记成"failed before the runner checked in; pod 已回收，无终态可抓"。
  实测 2026-11-08：`astock-daily-increment`（policy 2659）自 19:21 起 07:17/08:00/08:08
  三次全红；用 job 自己的脚本 ConfigMap + 同款 1Gi limits 复现，补 import 后同一 pod
  顺利通过并打印 `universe: 5203 codes / symbols: 5203`。
  为什么先前的带 `--db-url` 试跑是绿的：`or` 短路，坏的那一侧从未被求值。

## [0.5.50] - 2026-11-08
- **fix(hotel_series)**: series 读取**按概念自身的 `entity_type` 约束**。实测 2026-11-08：
  8,500 条 `stock` 型观测挂在**行业**实体 id 上（错标的港股/ETF 代码所致），而
  `hotel_series` 支持"任意 code + 任意 entity_id"，只按 `concept_id`/`entity_id` 过滤——
  拿行业 id 去查股票概念就能取到某公司的行情/财报。现查询固定加
  `o.entity_type = :cet`（取 `concepts.entity_type`），让"概念是什么类型、观测就必须是什么类型"。
  与 0.5.49 前的写入口修复（五个 ingest 的 code→entity 映射改 JOIN entities）一起，
  把这条错配数据在**读、写两端**都挡住。

## [0.5.49] - 2026-11-08
- **fix(source-credential-hygiene)**: 0.5.48 把 DSN 从**命令行**拿掉后，实测真派发
  （`cheap/crawl-policy-2659-20261108071716`）发现它仍在 **Job spec 的 env `value:`** 里——
  `_common_env()` 把 `clusters.runtime_hints.database_url/redis_url`（cheap 行里带明文口令）
  当字面值写进去，任何 `get pods -o yaml` 都能读到。现改为**一律 `secretKeyRef`**
  （`fd-db-secret` 的 `DATABASE_URL` / `REDIS_URL`），并在 cheap `scraw` 补齐该 secret；
  带回归单测（断言两个凭据变量都是 secretKeyRef、无字面 `value`）。
  `runtime_hints` 的明文值从此不再进入任何 pod 规格（保留字段以兼容旧集群，但不再被使用）。

## [0.5.48] - 2026-11-08
- **fix(stop-passing-dsn-on-argv)**: direct-script Job 的**命令行里带着库口令**。
  `_direct_manifests()` 拼的是 `… <script> <args> --db-url "$FD_OPEN_DATA_MCP_DATABASE_URL"`，
  展开后口令进入 Job 的 `spec.containers[0].command`（任何 `get pods` 都能读到）**和**
  `/proc/<pid>/cmdline`。0.5.46 只修了日志，argv 是漏下的那份。现改为：命令行只留脚本与
  policy 参数，DSN 从容器 env（`fd-db-secret` → `FD_OPEN_DATA_MCP_DATABASE_URL`）取；
  脚本在 `--db-url` 缺省时读 env，两者皆无则显式报错退出（本地/回补仍可用 `--db-url`）。
  带回归单测（断言 Job 命令不含 `--db-url`/`postgresql`）。

## [0.5.47] - 2026-11-08
- **feat(add-yfinance-proxy-lane)**: yfinance 在本舰队**完全不可用**——所有直连出口都被封。
  实测 2026-11-08：`fd-deploy`(124.220.7.175)/`cheap3` 回 429（23 字节 `Edge: Too Many Requests`，
  `server: ATS`），cheap 七个节点回 403（3369 字节页），fd-master 上 `urllib`/`requests`/`curl_cffi`
  （chrome124/chrome/safari17_0/无指纹）**一律 429**。**换节点 IP 无效**（9 个出口、两种封锁形态、零成功）。
  有效的路是**代理池 + 浏览器指纹**：`curl_cffi impersonate="chrome124"` 经池内代理对 chart 端点
  45 个里 27 个回 200（1718 字节），crumb 端点也 200，而同一代理下 `urllib` 是 429。
  但管线只在 contextvar 里发布 per-attempt 代理并 patch `requests`/`httpx`——**yfinance 1.x 走
  `curl_cffi`，不在 patch 覆盖内**，所以它一直直连等死；把 curl_cffi session 传进
  `Ticker(session=…)` 也不够（它自己的 cookie/crumb 连接仍失败，`CONNECT tunnel failed 403`）。
  修法：新增 `proxy.injection.current_proxy_url()` 公开本次 attempt 的代理，
  `run_yfinance` 在调用前 `yf.set_config(proxy=…)`（并置 `HTTP(S)_PROXY`），调用后**恢复**（含恢复为
  None，防止代理泄漏到下一次 fetch）；`FD_YF_PROXY` 覆盖优先，无代理则维持直连旧行为。
  实证：同进程先直连（`YFRateLimitError`）、再设代理 → 23 行真实 OHLCV。注意 `set_config` 是进程级，
  并发不同代理会互相干扰（爬虫 Job 单 fetch 单进程，风险已记录）。

## [0.5.46] - 2026-11-08
- **fix(db)**: 启动日志把数据库口令打成明文——`Database initialized: postgresql+psycopg2://fd:<口令>@…`
  每一轮 reconciler tick 都会写进 pod 日志，而 pod 日志的传播面（kubectl logs/GHA 产物/粘贴记录）
  远大于 Secret 本身。现统一走 `_redact_dsn()`（`make_url(...).set(password="REDACTED")`），
  带回归单测；无口令的 DSN（sqlite）原样输出。

## [0.5.45] - 2026-11-08
- **fix(fix-census-refresh)**: 0.5.44 的 SAVEPOINT 修复是**假绿**（结构对、语义没生效）。
  0.5.44 在 `refresh_census` 开头取一次 `conn = session.connection()`，之后才进
  `session.begin_nested()`。SQLAlchemy 2.0 下这样写**根本不发 `SAVEPOINT` 语句**：会话已经
  加入那条已在跑的事务，`begin_nested()` 变成静默 no-op。于是隔离看着有、实际没有——死分片
  依旧 abort 整个事务，后续 `_upsert` 仍报 `InFailedSqlTransaction`，census 照样整轮丢弃。
  真 PG 实测三种写法：`session.connection()` 在 nested 外 → 0 条 SAVEPOINT、事务死；
  在 nested 内 → `SAVEPOINT` + `ROLLBACK TO SAVEPOINT` 齐全、事务存活。
  修法：每条腿都在 `with session.begin_nested():` **内部**取连接，并把这条顺序约束写进
  `refresh_census` 注释；`_obs_month_distribution` 改收 session、自带 SAVEPOINT 并独立成腿
  （原先它跟在本地腿后面，本地腿一失败连分布一起跳过）。
  回归测试升级为**断言 SQL 里真出现 SAVEPOINT**（只断言 `begin_nested()` 被调过挡不住这种
  静默降级——这正是 0.5.44 漏网的原因）。
  真 PG 死分片演练（生产库，给 `shard_xinru2` 注入 `UndefinedTable`）：该店记下错误文本、
  `gz_master` 与 `shard_xinru3` 照常提交、`sampled_at` 前进，共 6 条 SAVEPOINT 语句。

## [0.5.44] - 2026-11-08
- **fix(fix-census-refresh)** ⚠️ 本条②的 SAVEPOINT 部分**实际未生效**，见 0.5.45：
  census 刷新自 2026-11-03 起静默失败，两个缺陷叠加。
  ① `data_census.approx_rows` / `total_size_bytes` 是 **int4**，本地库在 2026-11-08 长到
  `2,873,810,944` 字节越过 2,147,483,647 上限 → `NumericValueOutOfRange: integer out of range`；
  两列改 `BigInteger`（迁移 `0017_census_bigint`，int4→int8 不重写表）。
  ② 修完①才露出的真因：`refresh_census` 捕获 probe 异常后**没有回到 SAVEPOINT**，而 PostgreSQL
  里失败语句已把事务置为 aborted，于是后续 `_upsert` 与 `commit` 全部报
  `InFailedSqlTransaction: current transaction is aborted`——**一个死分片（`shard_xinru3`）
  把整轮 census 连刚算好的本地行一起丢掉**。现每条腿（本地统计 / 店列表 / 每个 shard probe）
  各自 `session.begin_nested()`，单腿失败只回滚自己。

## [0.5.43] - 2026-11-08
- **fix(add-crawl-job-deadline)**: kubectl 分支把「配额饥饿的 deadline Job」读成**假成功**。
  真集群实测：这种 Job 的 `status` 里 `active/succeeded/failed` 全为 null，只有
  `FailureTarget`+`Failed` 两条条件；而 `kubectl -o jsonpath="{.status.active} {.status.succeeded} ..."`
  对**缺失字段什么都不输出**，token 序列左移，`succeeded` 位被 `DeadlineExceeded` 占住 →
  `poll()` 返回 `success`。0.5.42 的 REST 分支是对的，只有本地 kubectl 分支错。
  修法：kubectl 分支改取 `-o json`，与两条 REST 分支共用同一个
  `_job_state(status)` 解析器（解析器只留一份，三条传输不可能再各自漂移）。

## [0.5.42] - 2026-11-08
- **fix(add-crawl-job-deadline)**: 无 pod 的 deadline 终止 Job 不再悬空。真集群实测发现：
  `activeDeadlineSeconds` 在 **pod 从未创建**（配额满位）时只写 `conditions`
  （`type=FailureTarget/Failed, status=True, reason=DeadlineExceeded`），**不写
  `status.failed` 计数**；`job_status()` 原先只读计数 → 返回 `unknown`，三条 `policy_runs`
  永远挂在 `running`（正是"删 Job 却留下悬空行"的同类陷阱）。现改为**先判条件后判计数**，
  并新增 `_failed_condition()`：`Failed=True` 条件本身即终态。kubectl 分支补抓
  `conditions[?(@.type=="Failed")].status`。

## [0.5.41] - 2026-11-08
- **add-crawl-job-deadline**: 爬虫 Job 加 `activeDeadlineSeconds` 硬上限（缺省 10800s，
  可经 `SCRAW_JOB_ACTIVE_DEADLINE_SECONDS` 覆盖）。`ttlSecondsAfterFinished` 只在 Job
  进入终态后计时，卡在 `Running` 的 Job 永不回收——2026-11-07 fd-deploy 事故即 16 个
  一次性 Job 卡 4h48m 把宿主压进 swap、拖垮 tailnet 与 `auth.finddatatech.cloud`。
  `poll()` 新增 `deadline_exceeded` 终态（按 `conditions.reason` 判定；真 K8s 写的是
  `type=FailureTarget`），收口 `policy_runs.detail` 记 `deadline exceeded after <N>s`
  并附 pod 证据，使「进程卡死」与「源站慢」在台账上可分。cheap 实跑探针 61s 转
  `deadline_exceeded` 验证。

## [0.5.38] - 2026-10-06
- **scope-rest-api**: 新增 REST JSON 面（`/scopes` CRUD + `/indicators/publish|unpublish`），
  包装既有 scoping 服务；Bearer `SCOPE_API_TOKEN` 鉴权（未配置 503 诚实禁用）；
  修复 `_known_values` 原生 SQL `deprecated=0` 在 PG BOOLEAN 列的潜伏缺陷
  （被吞错误毒化事务→建 scope 全 InFailedSqlTransaction；改 `= FALSE` + 降级
  except 补 rollback）。wire（柏讯）交付私有 scope 全链由此点亮（2026-10-12）。


### Added (indicator-caliber-unification)

- **`list_registry_entries` MCP tool: full-registry enumeration channel.** Pages
  through ALL registered entries — verified and not — ordered by
  (source_db, native_code), each row carrying the authoritative `verified`
  flag, with an optional status filter. This is the tiered-browsing channel
  (ADR-0002): the catalog/search/read verified gate (`list_concepts`,
  `ai_search`, reads) is untouched and still excludes unverified entries.

## [0.5.37] - 2026-10-05

### Fixed (scraw-runner-image-regression follow-up)

- **bulk_ingest scripts: upsert target aligned with the source-aware key**.
  The three `bulk_ingest_*` scripts still used the pre-0002 five-column
  `ON CONFLICT (concept_id, entity_type, entity_id, date, granularity)`
  target, which no longer matches any unique index since the
  source-aware key `uq_sem_obs (…, source_used)` — every upsert batch
  failed with "no unique or exclusion constraint matching the ON CONFLICT
  specification" (surfaced 2026-10-05 once the scripts could launch again;
  policies 7/8 had been masked by the missing-script failure since
  2026-09-29). `source_used` is now part of the conflict target; verified
  against production (1816 fund-NAV observations upserted).

## [0.5.36] - 2026-10-05

### Added (scraw-runner-image-regression)

- **Failure diagnosis for runs that die before checking in**: when a
  crawl Job's container terminates before the runner reports (the
  2026-10-01 w3lib-regression failure mode), the reconciler now
  synthesizes a non-empty error summary from the pod's terminal state
  (exit code, reason, last 30 log lines, capped at 2000 chars) into
  `policy_runs.detail` / `crawl_runs.error_head`. Runner-reported runs
  are never overwritten; reclaimed pods get a non-empty fallback note.
- **scripts/ now ships in the sdist** (`recursive-include scripts *.py`):
  the scraw fleet image extracts direct-script policy sources
  (`bulk_ingest_*`) from the sdist to `/app/scripts` (see the scraw
  Dockerfile), un-blocking direct policies 7/8 which have been failing
  with "script not found" since 2026-09-29.

## [Unreleased]

### Changed (schema-drift-closure)

#### 15 externally-bootstrapped tables adopted into the migration chain

`alembic check` revealed 15 model tables living entirely outside the
versioned chain (crawl control-plane family, discovery mirrors, scopes,
registry_entries), masked by the EXTERNALLY_MANAGED_TABLES bridge since
74963fa. They are now chain-owned:

- **0006_control_plane_adoption**: guarded `CREATE ... IF NOT EXISTS` for
  the 12 control-plane tables, DDL taken from the live authoritative shapes
  (per-table reconciliation in the change's reports/adoption-diff.md), so a
  bootstrapped production migrates as a verified no-op while a fresh
  database builds the complete schema from the chain alone.
- **0003** gains a guarded `registry_entries` create before its
  FK-carrying `registry_indicator_embeddings` (fresh replays need the FK
  target; the "deliberately not created here" convention is officially
  overturned). Downgrade still never drops it — the registry pipeline owns
  the data.
- Models aligned to production truth: NOT NULLs, deployed constraint/index
  names (`crawl_items_pkey`, `crawl_identities_source_account_alias_key`,
  …), the missing `crawl_items.run_id` FK, DESC composite indexes, and
  drifted defaults (`requested_by 'console'`, `max_attempts 2`,
  `params '{}'`). scopes keeps the deployed UNIQUE CONSTRAINT
  (`scopes_name_key`) — the model's unique-index form was the drift.
- `alembic/env.py` exemption narrowed to the two no-model registry tables;
  drift detection is back for all 13 model tables (model edits without a
  revision fail `alembic check` again).
- CI no longer bootstraps registry_entries: the chain itself satisfies the
  precondition (Build Check runs upgrade → check → vocabulary on a bare
  pgvector database).

Verified end-to-end 2026-10-01: empty-DB upgrade, `alembic check` green,
catalog-digest no-op proof on a bootstrapped database, and a post-rollout
`\d+` export of the authoritative database byte-identical to the
pre-rollout snapshot.

## [0.5.35] - 2026-10-01

### Fixed (image-slimming)

#### FastembedEncoder implements get_embedding_dimension

0.5.34's encoder missed the SentenceTransformer dimension probe that
semantic_search calls on every query — every semantic_search call on the
fastembed backend failed with AttributeError. The probe is now computed
once and cached, and a surface-contract unit test pins every model.* method
the search paths call so a backend swap cannot silently miss one.

## [0.5.34] - 2026-10-01

### Changed (image-slimming)

#### Browser stack split out of the `[data]` extra

`scrapling` and `playwright` move from `[data]` to a new `[browser]` extra.
Nothing in the crawl fleet imports them (verified 2026-10-01: zero
`scrapling` references in fd-open-data-mcp and scraw-fd-open-data-mcp;
playwright only in the uncalled `scraping/browser.py` helper, which also
speaks the remote-browser `BROWSER_CDP_URL` mode), and the industry line
installs its own playwright+chromium stack in its image. Since the fleet
image resolves `fd-open-data-mcp[data]` from PyPI at build time, keeping
the browser packages in `[data]` would have silently re-fattened every
future fleet rebuild. `pip install fd-open-data-mcp[data]` no longer pulls
the browser stack; `pip install fd-open-data-mcp[data,browser]` restores
it where browser rendering is actually needed.

#### Embedding backend chain: fastembed (onnxruntime) first, sentence-transformers fallback

`[search]` now installs `fastembed`, serving the same MiniLM weights
(`sentence-transformers/all-MiniLM-L6-v2`) as official ONNX exports on a
~15MB onnxruntime instead of torch's ~195MB compressed runtime;
`sentence-transformers` moves to `[search-legacy]` for exotic
`FD_MCP_EMBEDDING_MODEL` values outside fastembed's catalog. The shared
facade (`fd_open_data_mcp.embeddings.model`) resolves fastembed first and
falls back to sentence-transformers; `generator.py` and
`semantic/entity_search.py` no longer instantiate `SentenceTransformer`
directly, so no backend bypass remains. The vector contract is unchanged —
same model identity, same 384 dims, `encode(str)` 1-D / `encode(list)` 2-D
semantics — and failures still surface naming the model.

#### Slim image runtime env cleanup

The plain (non-torch) Dockerfile no longer bakes the tuna pip index into
the runtime image's ENV; CN mirrors are build-time concerns only.

## [0.5.33] - 2026-09-29

### Fixed (panel-indicator-observatory)

#### Domain graph view crashed on PostgreSQL

The registry-domain neighborhood query aggregated any-verified with
`max(verified)` — PostgreSQL has no `max()` for boolean (SQLite, the test
backend, accepted it, so unit tests stayed green). Replaced with the
portable `count(*) FILTER (WHERE verified) > 0`; verified against the live
production database.

## [0.5.32] - 2026-09-29

### Added (panel-indicator-observatory wave 2)

#### Scope management pages

`/panel/indicators/scopes` completes the observatory board now that
`registry-transparent-read-and-scope` has landed: scope list with rules and
7-day hit totals, create / edit / delete forms over the four allow-list
dimensions (source_dbs / domains / semantic_codes / native_codes,
comma-separated, blank = unconstrained), and a per-scope detail page with
per-day hit statistics. The routes call the same `scoping` service functions
the MCP scope tools call (no HTTP hop), so a scope created in the panel is
immediately usable via the tools; empty-scope and reserved-name refusals
surface as explicit errors.

## [0.5.31] - 2026-09-29

### Added (registry-transparent-read-and-scope)

#### Transparent federated reads (concept-fetch delta)

`read` / `read_series` now accept registry-only indicators — pass the
semantic_code (or native_code) as `concept_id` — and route them, by the
entry's source_db, to the business-mcp domain read tool (`yearbook_read` /
`wb_read` / `gta_read` / `china_city_panel → city_read`), projecting results
into the standard read row shape; provenance travels in `source_used`.
Rollout flag `FD_MCP_FEDERATED_READ` defaults OFF (staged per-domain
rollout; endpoint via `FD_MCP_BUSINESS_URL`). Failure semantics are loud and
explicit: `federated_read_disabled`, `no_read_channel` (names the source),
`federation_unavailable` (with a short retry cooldown). Local concept reads
never touch the federation path.

#### read_via hints (open-data-catalog delta)

Catalog (`list_concepts`) and search (`ai_search` / `semantic_search` /
`semantic_search_unified`) outputs attach to every registry entry a
`read_via {tool, args}` hint naming the domain tool + native-code argument;
sources without a read channel carry `read_via: null` rather than a wrong
hint. Local concepts are never labeled registry-only.

#### Retrieval scopes (indicator-scope)

Named allow-list scopes `{source_dbs, domains, semantic_codes, native_codes}`
stored in `fd_open_data` (`scopes` / `scope_bindings` / `scope_stats`,
migration `0004_scope_tables`). Management tools `scope_create` /
`scope_list` / `scope_update` / `scope_delete` (empty-scope validation
against live tables with the match count returned; unknown code values
warn without blocking; `unscoped` reserved). All search/read tools take an
optional `scope` parameter — out-of-scope reads return an explicit
`out_of_scope` response, scoped responses disclose `{name, summary}`, scoped
emptiness is explainable. Caller default bindings via `scope_bind_caller` /
`scope_unbind_caller` (opaque caller key from `X-FD-Caller` or
`FD_MCP_CALLER_KEY`; explicit parameter and the reserved `unscoped` always
win). Per-(scope, day) hit counters served by `scope_stats`. The same
scope semantics run inside business-mcp's domain search tools
(single-source: admit → disclose+count, exclude → explainable empty).


## [0.5.30] - 2026-09-29

### Added (panel-indicator-observatory)

#### Indicator observatory board

New panel board「指标 Indicators」under `/panel/indicators/*` — the observability
surface over the 57k-entry unified indicator registry and the concept layer.
Reads the same tables the MCP tools serve through the same session (a view,
never a fork); sits behind the existing auth/role gate like every board.
Five views:

- **Registry table** (`/panel/indicators`): domain / source_db / verified /
  keyword filters, server-side paging (50/page), unverified entries visible
  with an unmistakable badge (ops caliber — distinct from the public
  catalog's verified-only gate).
- **Concept family tree** (`…/families`): families → member concepts, each
  with its binding count; per-family drill-down.
- **Relation browsing** (`…/relations`): cross-source binding list (native
  codes) + mappings searchable by external vocabulary, SKOS relation type
  and keyword; per-indicator pages list the registry anchors sharing a
  semantic code — the indicator's cross-source equivalence set.
- **Coverage** (`…/coverage`): registered/verified per source database and
  domain as grouped SVG bars + tables — the identical GROUP BY
  `registry_coverage` runs on the same database, so the two surfaces agree
  at the same moment.
- **Interactive relation graph** (`…/graph`): vendored zero-build
  vis-network 9.1.9 (UMD, ~675KB) served from the panel's same-origin
  static mount — no CDN, no build step; bounded neighborhood views by
  family / indicator / domain with server-side depth (≤2) and node (≤200)
  caps; node detail panel; theme tokens and bilingual labels follow the
  panel's active settings; degrades to a server-rendered relation listing
  inside `<noscript>` when client scripting is unavailable.

New module `panel/observatory.py` (fail-soft registry reads like
`semantic.registry_catalog`), `charts.grouped_bar_geometry`, 15 tests in
`tests/test_panel_indicators.py` covering every spec scenario incl. the
same-source fork guard and the self-hosted/no-CDN page-source contract.
Scope management pages are deliberately out of this wave — they wait for
change `registry-transparent-read-and-scope` to land.

## [0.5.29] - 2026-09-29

### Fixed (mcp-search-engine-overhaul P0)

#### semantic_search resolves its model by name

`semantic_search.py` loaded the embedding model from a hardcoded dev-machine
cache path, so every `semantic_search` / `semantic_search_unified` call in the
production container failed with a filesystem error. Model resolution now
lives in `fd_open_data_mcp/embeddings/model.py`, resolves by name (env
`FD_MCP_EMBEDDING_MODEL`), and failures surface as a tool error naming the
model.

#### Each tool registered exactly once

`semantic_search.py`, `ai_search.py` and `entity_graph_tools.py` carried
module-level `@mcp.tool()` registrations that re-registered (and overrode)
the server-level definitions the first time a tool call lazily imported them.
Those registrations are gone — `server.py` is the only registration point.

#### Retrieval caches actually survive across calls

The graph manager, entity semantic search and their caches (graph TTL,
embedding query cache) were rebuilt per call, making the 300s TTLs useless.
Both engines are now process-wide lazy singletons (`fd_open_data_mcp/engines.py`),
shared with fd-find-data-business-mcp's delegated `graph_search`.
`GRAPH_CACHE_TTL` / `EMBEDDING_CACHE_SIZE` are wired; `CACHE_ENABLED` /
`SEARCH_RESULT_CACHE_TTL` gate a new search-result TTL cache whose hits are
marked `"cached": true`. Write tools (`update_entity` / `add_entity` /
`add_relationship` / `update_concept` / `re_embed_concept`) invalidate caches
immediately instead of waiting out the TTL.

#### ai_search value layer runs on SQLite dev databases

Layer 3 used PostgreSQL-only `ANY()` / `DISTINCT ON`; now portable expanding
`IN` + `ROW_NUMBER()` window, verified end-to-end on SQLite.

#### Housekeeping

- `semantic_search` / `semantic_search_entities` / `semantic_search_unified`
  now return `{cached, count, results}` envelopes so cache hits stay
  distinguishable.
- Hardcoded DSNs (with a stale password placeholder) removed from
  `scripts/migrate_add_vector_search.py`, `scripts/generate_entity_embeddings.py`
  and `scripts/migrate_entity_sync_schema.py`; they now require
  `FD_OPEN_DATA_MCP_DATABASE_URL`.
- Dead `.env.example` keys removed (`EMBEDDING_MODEL` → `FD_MCP_EMBEDDING_MODEL`,
  `CACHE_TTL_HOURS` → `SEARCH_RESULT_CACHE_TTL`).

### Added (mcp-search-engine-overhaul 3.x/4.x) — shipped 2026-09-29 as sha-6ff2017..a0f2dec

- **pgvector + HNSW vector engine** (ADR 0001): alembic 0003 adds
  `embedding_vec vector(384)` columns + batched backfill + HNSW cosine
  indexes + `registry_indicator_embeddings`; reads execute inside
  PostgreSQL (`FD_MCP_VECTOR_BACKEND=pgvector`, live since sha-a0f2dec).
  Dual-read verification passed (concept 1.00/1.00, entity 0.983/1.00
  Jaccard/Kendall). Two pgvector defaults proved unsafe and are now set
  per-session: `hnsw.ef_search=200` (beam missed exact neighbors in
  duplicate clusters) and `hnsw.iterative_scan=relaxed_order` (filtered
  queries returned EMPTY when the beam neighborhood failed the filter).
- **Registry corpus in semantic search**: all 57,552 registry entries
  embedded (57,552 rows / 1,052 s, local inference; 1 all-empty skipped);
  verified-gated at query time (live JOIN, 3,420 visible, 54,132
  embedded-but-invisible); registry-only indicators surface with
  `result_type: "registry"` (verified live: "grain output" → city panel
  Grain Output ranks first).
- **Transitional matrix backend** (`FD_MCP_VECTOR_BACKEND=matrix`):
  in-process numpy cache with `VECTOR_MATRIX_TTL`, equivalence-tested
  against the json backend on SQLite.
- **sqlalchemy pinned <2.1**: 2.1.x (fresh on the build mirror) removes the
  psycopg2 fallback for `postgresql://` and crashed the migrate-schema
  initContainer; pin restores the tested dialect.
- Reference k8s manifest aligned with the live fd-official-web deployment
  (was a stale zihan-era copy: 512Mi / harbor.local / NodePort 30801).

## [0.5.16] - 2026-09-18

### Fixed

#### Requests that can never succeed no longer get retried

The retry, circuit-breaker and ranking machinery could express "this route is
unhealthy" but not "this request is impossible", so a missing endpoint was
classified `transient` and retried against proxies it had nothing to do with.
Between 2026-08-30 and 2026-09-16 the fleet logged **5,277,961** `has no
callable` attempts, and on 09-16 it issued roughly 266,000 failed fetches in 24
hours with **zero** successes.

- **`permanent` failure class** — a new `ban_rules` classification, seeded for
  the `has no callable` family. A permanent failure is not retried and does NOT
  touch a proxy circuit or the outcomes stream: the exit is not at fault.
- **`fetch/capability.py`** — static, no-I/O resolution of `(source, command)`.
  Whether a callable exists is decidable without spending a network request.
- **Entity-domain agreement** — rules matched on column *name* alone, so every
  akshare endpoint with a `收盘`/`close` column bound to `price.close`/`stock`:
  options, futures, bonds, funds and indices among them (concept 234 reached
  **117** dispatch-eligible bindings). A proposal is now refused when the
  function's declared domain and the rule's concept domain disagree. Domain
  comes from a command prefix, an instrument word, or the source; instrument
  words are checked before prefixes, or `stock_board_industry_index_ths` is
  claimed by `stock_`.

### Changed

- **Binding eligibility needs evidence, not just a score** — the dispatch gate
  was satisfiable by `confidence >= 0.6` alone and the rule table emits
  0.85–0.9, so every machine proposal was live and the review step gated
  nothing. An `llm`-provenance binding now requires confirmation; confidence is
  a floor, never a substitute. Ships **report-only**
  (`FD_BINDING_ELIGIBILITY_GATE`) so a mis-written gate cannot narrow a live
  crawl before it is reviewed.
- **`confirm_by_rule` / `verify_functions_by_rule`** — rule-based binding
  confirmation and function verification. Both require positive evidence
  (domain agreement AND resolvability) and refuse rather than assume. A
  confirmation records the distinct provenance `rule-confirmed`.
- **Bounded failover chain** — `FD_CRAWL_CHAIN_MAX` (default 8). Candidates
  past the bound are reported as not-attempted rather than dropped silently.
- **Permanent-path suppression** — a `(concept, function)` whose recent
  outcomes are all permanent is excluded outright rather than reordered
  ("last" is still "attempted"). Derived from `fetch_log`, computed once per
  plan, clearable by data. Suppression is **cluster-independent** because a
  missing callable is missing from every egress — unlike demotion, which is
  route health and stays per-cluster.

Note there are **two** gates: a binding is dispatchable only when the binding
is eligible AND its function is verified. Of the 536 bindings confirmed by the
new rule, 508 sat on `verified = false` functions and remained unreachable.

## [Unreleased]

### Added

#### Multi-source observations (`add-multi-source-observations`)

- **Sources coexist per observation point** — the `semantic_observations`
  unique key gains `source_used` (`concept, entity, date, granularity,
  source`), so WorldBank's China-2024 GDP and NBS's are stored as separate
  attributed rows instead of first-writer-wins. Values are never merged.
- **Query-time source selection** — a plain `read` returns the highest-ranked
  source's row (`source_rankings`, effective on the next read with no row
  rewrites); `read(source=...)` pins cache reads and dispatch to one source;
  `read(all_sources=true)` returns every held row per point, best-ranked
  first, without dispatching. Same parameters on the MCP `read` tool and the
  `fd-open-data-mcp read` CLI (`--source`, `--all-sources`).
- **Source-scoped watermarks** — `since_last` crawl plans compute their
  watermark over the policy's `source_filter` when set, so a second source's
  backfill plan starts from what that source itself holds.
- **Coverage counts points, not rows** — `/panel/data` and `data_stats`
  report distinct observation points plus a per-concept `sources` count, so
  coexistence never inflates coverage.
- **Online migration** — `fd-open-data-mcp migrate` swaps `uq_sem_obs` to the
  source-aware key (Postgres: concurrent index build + constraint swap under
  an advisory lock; SQLite: table rebuild). Data-preserving by construction
  (the relaxed key admits every existing row). `migrations/
  007_multi_source_observations[.rollback].sql` is the manual runbook; the
  rollback dedupes to the highest-ranked source per point and reports what
  it removed. Requires `scraw-fd-open-data-mcp` deployed against the same
  schema (its writer names the 6-column conflict target).

#### New Data Source Adapters (21 sources)

- **NBS GDP** (`nbs-gdp`): National Bureau of Statistics macroeconomic data
  - Quarterly GDP data with akshare fallback
  - Monthly CPI, PPI, PMI indicators
  - Direct API integration with stats.gov.cn

- **CISA Industry** (`cisa-industry`): China Iron and Steel Association data
  - Steel production statistics
  - Market statistics and pricing

- **AMAC Fund** (`amac-fund`): Asset Management Association of China
  - Fund registration and statistics
  - Manager registration data

- **SHFE Futures** (`shfe-metal-futures`): Shanghai Futures Exchange
  - Metal futures pricing (Cu, Al, Zn, Pb, Ni, Sn)
  - Volume and open interest data

- **SAC Securities** (`sac-securities`): Securities Association of China
  - Securities trading statistics
  - Market turnover data

- **Agriculture** (`agriculture`): Dalian Commodity Exchange
  - Agricultural futures pricing (soybean, corn, PP, JB)
  - Volume and open interest tracking

- **CME Agricultural** (`cme-agricultural-futures`): CME Group
  - Grain futures pricing (corn, wheat, soybean, oat)
  - Soy meal data with USD→RMB conversion

- **Chemicals** (`chemicals`): SCI99 chemical industry data
  - Basic chemical product prices (PVC, methanol, ethylene, propylene)
  - Chemical industry PMI and production indices

- **Electronics** (`electronics`): CEIA electronics association
  - Semiconductor industry statistics
  - Electronic circuit, display panel, consumer electronics output

- **Nonferrous Metals** (`nonferrous`): CNIA non-ferrous metals data
  - Aluminum, copper, lithium pricing and inventory
  - Import/export statistics

- **Flowers KIFC** (`flowers-kifc`): Kunming flower auction center
  - Daily flower auction prices (rose, lily, orchid, tulip)
  - Trading volume and buyer statistics

- **Financial Platforms** (`fin_platforms`): Wind financial terminal
  - Market benchmark indices (SH50, SZ300, HSI, NASDAQ, S&P500)
  - Sector performance tracking
  - Fund ranking statistics

#### Infrastructure Improvements

- Added `errors.py` module with custom exception types
- Enhanced `runner.py` with lazy loading for all adapters
- Added comprehensive test suite in `tests/test_runners.py`
- Updated manifests with `fetch.runner` and `ranking_seed` fields
- Integrated caching layer for improved performance

#### Documentation

- Updated README with usage examples and rate limit policies
- Created deployment guide in `docs/DEPLOYMENT.md`
- Added comprehensive task tracking in OpenSpec workflow

### Changed

- Updated `pyproject.toml` to include new dependencies (requests, beautifulsoup4, scrapling)
- Enhanced manifest schema compliance with fd-open-data-protocol
- Improved error handling across all data source adapters

### Fixed

- Resolved import errors for new adapter modules
- Fixed runner routing for all 21 new data sources
- Corrected manifest YAML syntax validation

## [0.5.15] - 2026-09-15

### Added

#### Semantic vocabulary core (two-level concept model + crosswalk)

- **Concept families** — new `concept_families` table, materialized from
  `fd-open-data-protocol`'s `vocabulary/concepts.yaml`, with `concepts.concept_code`
  linking every Variable to exactly one family. Variables without an explicit
  family get one derived from the concept code.
- **`consume-concepts` repointed** from the (absent) `fd-entities-indicators`
  sqlite to the protocol vocabulary — seeding now works on a clean checkout.
- **Concept crosswalk** — new `concept_mappings` table asserting Variable ↔
  external-vocabulary equivalences (SKOS relation, confidence, provenance,
  review state), plus `crosswalks/*.yaml` ingestion.
- **External entity anchors** — `wikidata` (QID) and `datacommons` (DCID) as
  per-source identifier sources, resolvable back to the local entity; manifest
  `entity_definitions[].metadata.external_ids` persists anchors at registration.
- **New tools** — `list_concept_families`, `record_concept_mapping`,
  `list_concept_mappings`, `import_crosswalks`; `list_concepts` now reports each
  Variable's family and filters by it; `resolve_entity` also resolves an
  external anchor to its entity; `get_entity`/`list_entities` expose anchors.
- **New CLI commands** — `import-crosswalks`, `list-concepts`,
  `list-concept-families`.

### Changed

- Requires `fd-open-data-protocol>=0.3` (for the shared crosswalk relation set).

## [0.1.0] - 2024-07-27

### Initial Release

- Core data source registry with 7 sources (akshare, yfinance, edgar, wbgapi, cn-report, cn-gov, world)
- Basic MCP server implementation
- Database schema for sources, functions, columns
- Initial test suite
