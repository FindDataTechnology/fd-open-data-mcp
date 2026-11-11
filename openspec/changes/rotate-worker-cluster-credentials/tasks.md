## 1. 止血：把禁用集群的凭据从挂载 Secret 摘掉

- [x] 1.1 从 `xinru/fd-mcp/worker-kubeconfigs` 移除非启用集群的凭据——验证：`/kubeconfigs` 下不再出现这些文件；`clusters` 表不变
  实测（2026-11-11）：不仅 `america.*` / `baidu.*`，`tencent.*` 与 `zihan.*` 同样非启用，一并摘除。
  `zihan` 在 `clusters` 表里**没有行**（纯孤儿凭据）。10 键 → 2 键 `['cheap.ca','cheap.token']`。
- [x] 1.2 同步核对 `fd-master/worker-kubeconfigs` 只保留 `enabled=t` 的集群（当前仅 `cheap`）——验证：键集合与 `clusters` 表 `enabled=t` 的行一一对应
  实测：摘除 `tencent.*`（`enabled=f`），4 键 → 2 键，与 `clusters` 表唯一 `enabled=true` 的 `cheap` 一致。
- [x] 1.3 写一条巡检：`enabled=f` 却在挂载 Secret 里出现的集群名，逐一点名——验证：故意留一个禁用集群的键，巡检必须报红
  `scripts/check_worker_credential_surface.py`，双向断言（禁用却在挂载里 / 启用却缺凭据），
  并区分「孤儿凭据」（表里没有该行）与「禁用集群」（有行但 enabled=false）。
  四向实测：违规 → rc=1 且点名；清理后 → rc=0 `OK`；启用却缺凭据 → rc=1 点名；
  目录缺失 / 两个输入都拿不到 → rc=2（**不把「挂载缺失」当成「没有凭据」**）。
  另用真实集群状态复跑：`clusters 表 4 个（enabled=1）`，仅 `cheap` mounted=yes，rc=0。


## 2. 轮换脚本（签新 + 写回 + 实测）

- [~] 2.1 `scripts/rotate_worker_credential.py` 已实现两种签法（`--mode bound|legacy`）+ 写回 + 实测；**`--apply` 真跑路径未执行**：它会让旧凭据立即失效，须等 bound/legacy 裁决与窗口。 已验证的部分：`--dry-run` 计划正确（rc=0）、`--verify-only` 对 cheap 返回 `OK PodList items=1`（rc=0）、对已摘除的 tencent 返回 `ERROR 控制面没有 tencent.token`（rc=2）。
- [x] 2.2 成对验收已实现：写回后先验新值，再验旧值（`旧值仍然可用——轮换未生效` 即 exit 1）；`--verify-only` 实测区分真值 `OK PodList` / 伪造值 `HTTP 401` / 空值 `HTTP 401`。旧值为空时（首次签发）显式跳过并说明，不假装验过。
- [x] 2.3 探针改为**本机直连目标 API**而非挂载 pod：worker API 本就可从操作机到达（今天全天如此用），省掉一次 pod 调度；读控制面 Secret 的 token 走 JSON 解析（避开 jsonpath 点号坑）。踩到并修掉两个真问题：①本机系统代理会把 100.64 网段送进代理 → 表现为 timeout 而非 401（已显式禁用代理）；②worker 的 kubeconfig 是 `insecure-skip-tls-verify`，探测强制校验证书会报 `CERTIFICATE_VERIFY_FAILED` 并被误读成"凭据失效" → 改为跟随该集群既有 TLS 姿态并在输出里注明。
- [x] 2.4 空操作判失败已实现：`write_back()` patch 后**读回比对**，不一致即记 `patch 报成功但读回不一致（空操作）`。另加一条：新旧值相同时报 `签出来的不是新凭据`——这正是今天"删 Secret 重建救活同一个 JWT"的教训。

## 3. 失效兜底：token 过期要可见

- [x] 3.1 派发失败且 HTTP 401 时，错误信息点名"worker 凭据可能已过期，运行 rotate_worker_credential"——验证：用过期 token 触发一次派发，日志出现该提示
  `reconciler.py` 的 `ClusterK8sClient._api()` 现在把 401/403 映射成 `WorkerCredentialError`，由纯函数 `worker_credential_hint()` 生成可执行提示（点名 `$KUBECONFIG_DIR/<cluster>.token`、列出三种成因、给出轮换命令）。401 与 403 分开：前者是凭据失效、后者是 RBAC/集群被禁用。
  实测（打真实 cheap API）：伪造 token → `WorkerCredentialError` 且提示含轮换命令；  真 token → `PodList items=1` 不回归。**这一条与 bound/legacy 无关**——legacy 也会因  SA 被重建或凭据被摘除而 401，所以先落地。

- [ ] 3.2 就绪/健康面暴露"凭据剩余寿命"（最近一次刷新时间 + `exp`）——验证：把刷新时间改早，看板/健康输出随之变化
- [ ] 3.3 CronJob 刷新节奏写进部署清单，且与 `--duration` 有明确倍数关系（刷新间隔 ≤ 寿命的 1/3）——验证：清单里的两个数值能被脚本读出来并断言满足比例

## 4. 文档与裁决

- [x] 4.1 在 `docs/` 记录 legacy 与 bound 两种 token 的取舍：legacy 不可撤销、bound 会到期；说明为何选 bound + 定期刷新
  `docs/adr/0004-worker-cluster-credential-lifecycle.md`。状态写 **Proposed**——裁决未定，
  文档只摊开取舍与前置条件（bound 的 3.x 可见性是**前置**，不是可选项），不假装已决。
- [x] 4.2 记录"吊销＝换 SA UID"的操作步骤与代价（会同时打断全部派发，须与派新 token 同一操作内完成）——验证：按文档在测试集群走一遍，旧值 401 且新值 200
  `docs/worker-credential-runbook.md` §2.1 写了完整命令序列与「没有回滚窗口」的代价；
  步骤来源是 2026-11-11 在 cheap-ts/tencent 实跑并验证过的（旧 401 / 新 200，UID
  `0fbd0b55…`→`db1f235a…`、`20a5609f…`→`5598ecbf…`），非推演。
- [x] 4.3 把"不得以 Job `Complete` 作为凭据可用判据"写进验收口径（没活可派时它恒为真）——验证：文档中出现该反例
  `docs/worker-credential-runbook.md` §4 有专门的反例表：Job Complete / patch 输出
  patched 或 no change / jsonpath 点号 / 本机 `kubectl --token` 的证书串扰，逐条写明为什么不能当判据。


## 5. 回归

- [ ] 5.1 `clusters.enabled=t` 的集群全部派发一次成功——验证：故意触发一轮派发，`launched` 非空且对应 Job 在该 worker 集群上 `Complete`
- [ ] 5.2 `enabled=f` 的集群不再被选中——验证：调度日志中不出现这些集群名
- [ ] 5.3 `openspec validate rotate-worker-cluster-credentials --strict` 通过——验证：返回 valid

## 6. 收口

- [ ] 6.1 归档——验证：`openspec archive rotate-worker-cluster-credentials --yes --strict` 成功
