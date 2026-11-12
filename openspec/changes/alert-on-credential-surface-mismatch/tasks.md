# Tasks

## 1. 判定搬进包里（唯一实现）

- [x] 1.1 `visibility/credentials.py` 增加 `mounted_clusters(dir)` 与
  `surface_problems(clusters, mounted)`（三种形态：孤儿 / 禁用却有 / 启用却缺），
  措辞与现有巡检脚本逐字一致——验证：脚本改调它之后，`scripts/` 下的既有输出不变
- [x] 1.2 `scripts/check_worker_credential_surface.py` 的本地 `check()` 改为薄封装——
  验证：`--kubeconfigs-dir/--clusters-json` 离线跑，输出与改动前同一条违规同措辞

## 2. 折进 scan

- [x] 2.1 `visibility/scan.py` 增加 `check_credential_surface(session)`：查 `clusters`
  表的 `{name: enabled}`，与挂载面比对，产出与到期同形的告警记录——验证：构造
  禁用却有凭据的目录 + 表，一轮 `scan_once` 产出一条并按 `credential-surface:…` 去重
- [x] 2.2 折进同一条消息：`_format_message` 增参，正文逐条点名，结尾给对应处置
  （孤儿/禁用 → 删挂载；缺失 → 签发）；未挂载时静默且 `credentials_checked=false`
  ——验证：四种情形各一条用例，且整体只 `send` 一次
- [x] 2.3 先证明能红：把新判定摘掉/改错（例如漏掉 disabled 分支）→ 对应用例报红，
  且只有该用例红——验证：注入前后各跑一次，记录失败名

## 3. 上线与收口

- [x] 3.1 `openspec validate alert-on-credential-surface-mismatch --strict` 通过
- [ ] 3.2 出镜像 → pod 内实测：真挂载（`cheap9` 一致）无告警、`credentials_checked: true`；
  空挂载仍 `false`——验证：两条 pod 探针输出
- [ ] 3.3 滚 `crawl-watcher-scan` 到新 tag，确认一轮定时 `SuccessCriteriaMet`
- [ ] 3.4 归档本 change，并补 `worker-credential-lifecycle` 的 Purpose
  （`--strict` 会把 archive 写下的 TBD 占位判红）

## 执行记录（2026-11-12）

**1.1 / 1.2**：`surface_problems` 的三条措辞与脚本原文逐字相同（脚本改调包内实现后，
`tests/test_scripts_resolve_repo_package.py`、`tests/test_worker_credential_lifetime.py`
的离线断言未改一字仍全绿）。包里同时加了 `mounted_clusters()`，只读文件名、不解析
JWT——它要回答的是"该有的在不在"，而 401 只在运行期才发生。

**2.1 / 2.2**：`tests/test_scan_credential_surface.py` 9 条覆盖一致静默 / 缺凭据 /
禁用却有 / 孤儿 / 未挂载 / 24h 去重 / 已告警抑制 / 与停滞合并一次推送 / 标题 32 字预算。
凭据族合计 **48 passed**（surface 9 + expiry 15 + 脚本 2 + lifetime + attribution + ratio）。

改动同时也修正了既有用例的场景缺口：`tests/test_scan_credential_expiry.py` 原来在
"挂载 `cheap.token`、`clusters` 表空"的前提下断言"不发消息"——那在新判定下是**孤儿**，
本就该告警。加了一条 autouse fixture 让挂了凭据的集群在表里存在（`enabled=true`），
场景自洽；`test_mounted_but_empty_is_not_green` 顺势加强为双向断言：到期那路明说
"没查过"，面那路报 `("cheap","missing")` 并给出 401 后果。

**2.3（诚实记录与偏差）**：注入方式是把 `surface_problems` 里的
`elif not clusters[name]:` 改成 `elif False:`（模拟"漏掉 disabled 这条判定"）。
实测**4 红 5 绿**，红的四个是：

- `test_disabled_cluster_with_token_alerts_and_says_delete`（目标用例）
- `test_dedup_uses_24h_and_records_per_cluster_subject`
- `test_surface_folds_into_one_message_with_stale`
- `test_title_leaves_the_count_visible`

比任务写的"只有该用例红"多三个，原因是后三条的 fixture 也挂了禁用集群 `cheap`，
它们共享同一个判定分支——这是**夹具性质**，不是断言写宽了：只要 disabled 这条判定
缺失，任何依赖它的场景都该红。还原后逐字校验（`diff` 空、`elif False` 计数 0）再跑
得 9 passed，备份删除。

**3.1**：`openspec validate alert-on-credential-surface-mismatch --strict` → valid。

## 不做

- 不在扫描里删除/修补凭据（只报告；处置归人）
- 不为"未挂载"告警
- 不新增第二条推送通道
