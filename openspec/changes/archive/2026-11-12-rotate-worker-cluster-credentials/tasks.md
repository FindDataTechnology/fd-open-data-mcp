> 裁决已定（2026-11-11）：**走 bound**，刷新放操作侧/CI，控制面不新增铸造权凭据。
> 2.1 已真跑并验证（含一次真实 Job 端到端）。剩 6.1 归档与 6.2 上线。
> 背景与取舍见 **[DECISION.md](DECISION.md)**。

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

- [x] 2.1 `scripts/rotate_worker_credential.py` 已实现两种签法（`--mode bound|legacy`）+ 写回 + 实测；**`--apply` 真跑路径**于 2026-11-11 对 cheap 执行（`--mode bound --duration 720h`）
  **走的是 bound，不是 legacy**：bound 是**加法**——签的是另一把凭据，worker 上那把 legacy
  token Secret 没动，所以旧值仍然可用；不存在"旧凭据立即失效"的中断，也就不需要窗口。
  （legacy 换 SA UID 会当场打断全部派发，那才需要窗口。）
  执行前先把控制面 Secret 原值备份到 `~/finddata/secrets/sa-token-backup/before-bound-20261111T200010.json`
  （`cheap.token` len 906 / sha256 前 12 位 `edd64fbf6765`），保留回滚路。
  成对实测：新值 `OK PodList items=1`；旧值 `OK PodList items=1`（bound 模式下**期望**
  仍然可用，脚本已按模式区分判定；见下面"顺手修掉的自身 bug"）。
  落地确认：`fd-master` / `fd-mcp` 两个 Secret 与 fd-panel pod 的挂载点
  `/kubeconfigs/cheap.token` 解出来都是 `bound`、剩余 30.0 天（kubelet 同步约 75s 后生效）。
  3.2 的健康面现在读作 `cheap enabled=true mounted=yes bound 寿命 30.0d，剩余 30.0d`，rc=0。
  **真派发复核**（不是只打只读 API）：用挂载点的值走 `ClusterK8sClient` 建一个 echo Job
  → `state=success`、日志 `BOUND_OK`、`delete=True`。
  中途一次 `deadline_exceeded` 是**探针设置问题**，不是凭据问题：`activeDeadlineSeconds=60`
  撞上 469MB 镜像拉取 67.5s（事件里 `Pulled ... in 1m7.553s` 之后 13s 就被 `Killing`）；
  同一条探针把截止时间放到 300s 后一次通过。记在这里，免得后人把那次红读成"bound 不可用"。
  **顺手修掉的自身 bug**：脚本原来无条件断言"旧值必须失败"，这在 legacy 模式对、在 bound
  模式错——照原样跑 2.1 会误报"轮换未生效"并 rc=1。已改成按模式判定，并新增
  `--retire-legacy`（仅 bound；删 worker 上的 legacy token Secret，让旧值真正失效，
  help 里写明**这会掐掉回滚路**）。本次**没有**执行 `--retire-legacy`：留着旧值当回滚路，
  等 bound 这条路跑一段时间再收。
- [x] 2.2 成对验收已实现：写回后先验新值，再验旧值（`旧值仍然可用——轮换未生效` 即 exit 1）；`--verify-only` 实测区分真值 `OK PodList` / 伪造值 `HTTP 401` / 空值 `HTTP 401`。旧值为空时（首次签发）显式跳过并说明，不假装验过。
- [x] 2.3 探针改为**本机直连目标 API**而非挂载 pod：worker API 本就可从操作机到达（今天全天如此用），省掉一次 pod 调度；读控制面 Secret 的 token 走 JSON 解析（避开 jsonpath 点号坑）。踩到并修掉两个真问题：①本机系统代理会把 100.64 网段送进代理 → 表现为 timeout 而非 401（已显式禁用代理）；②worker 的 kubeconfig 是 `insecure-skip-tls-verify`，探测强制校验证书会报 `CERTIFICATE_VERIFY_FAILED` 并被误读成"凭据失效" → 改为跟随该集群既有 TLS 姿态并在输出里注明。
- [x] 2.4 空操作判失败已实现：`write_back()` patch 后**读回比对**，不一致即记 `patch 报成功但读回不一致（空操作）`。另加一条：新旧值相同时报 `签出来的不是新凭据`——这正是今天"删 Secret 重建救活同一个 JWT"的教训。

## 3. 失效兜底：token 过期要可见

- [x] 3.1 派发失败且 HTTP 401 时，错误信息点名"worker 凭据可能已过期，运行 rotate_worker_credential"——验证：用过期 token 触发一次派发，日志出现该提示
  `reconciler.py` 的 `ClusterK8sClient._api()` 现在把 401/403 映射成 `WorkerCredentialError`，由纯函数 `worker_credential_hint()` 生成可执行提示（点名 `$KUBECONFIG_DIR/<cluster>.token`、列出三种成因、给出轮换命令）。401 与 403 分开：前者是凭据失效、后者是 RBAC/集群被禁用。
  实测（打真实 cheap API）：伪造 token → `WorkerCredentialError` 且提示含轮换命令；  真 token → `PodList items=1` 不回归。**这一条与 bound/legacy 无关**——legacy 也会因  SA 被重建或凭据被摘除而 401，所以先落地。
  ⚠️ **未上线**：线上 reconciler 是 `sha-5539dc5`（`fd_open_data_mcp.__version__ == 0.5.61`），
  这个版本早于本分支。于是"401 会说人话"目前只在代码里有。解锁见 6.2。

- [x] 3.2 就绪/健康面暴露"凭据剩余寿命"（最近一次刷新时间 + `exp`）——验证：把刷新时间改早，看板/健康输出随之变化
  落在既有的 `scripts/check_worker_credential_surface.py` 上（不新造一个面）：新增
  `credential_lifetime(token)` 从 JWT 的 `iat`/`exp` 读出 kind/寿命/剩余，`--json` 供看板与告警消费。
  三态分开：`bound`（有 exp）／`legacy`（JWT 无 exp ⇒ **永不过期**，这是风险不是健康）／
  `unknown`（不可解，不假装知道寿命）。判据与 3.3 同一刻度：**剩余 < 寿命 1/3 即报**
  （= 刷新没在跑，且比"已过期"早得多），过期另报一条。
  验证（三层）：①`tests/test_worker_credential_lifetime.py` 8 个用例，含"把刷新时间改早 → `ok` 翻转、
  剩余值变化"（3.2 的原话验收）；②反向验证：把 `stale` 改成恒 False（最典型的假绿写法），
  两个用例真红；③**真实生产凭据**：拿 fd-mcp 挂载里的 cheap 真 token 跑 → `永不过期（legacy，无 exp）`，
  `--json` 里 `expires_at=null` —— 这个事实以前任何看板都看不到。
- [x] 3.3 刷新节奏与 `--duration` 的倍数关系写进可执行物，且能被脚本读出来断言（刷新间隔 ≤ 寿命 1/3）
  **裁决（用户 2026-11-11 授权我定）：不给控制面新增铸造权凭据，刷新放在操作侧/CI。**
  理由：签 bound token 要 `create serviceaccounts/token`，而控制面握着的 worker 凭据明确没有
  这个动词（`auth can-i create serviceaccounts/token --as=…/crawl-worker` → **no**；对照组
  `create/delete/get jobs` → yes）。若为自动刷新给控制面加一把铸造权凭据，那把凭据本身也得
  长期有效、也要被刷新——问题只是往上挪一层，还多一把更强的常驻凭据。
  所以：控制面永远只有"能建删 Job"这一把；铸造在集群外用运维已有的 admin kubeconfig 完成，
  只把签好的 token 写回 Secret。代价是刷新依赖外部定时器——而 3.2 的 STALE 告警就是它的眼睛
  （剩余 < 1/3 时先亮，比 401 早一个刷新周期）。
  落地三件：
  - `scripts/check_credential_refresh_ratio.py`：寿命从轮换脚本的 `--duration` 默认值读，
    间隔从工作流 cron 读，断言 `间隔 × 3 ≤ 寿命`。两个数字分居两个文件，正是它们会漂的原因。
    当前实测：寿命 720h / 间隔 168h（`17 3 * * 1`）= **倍数 4.3**，OK。
  - `.github/workflows/refresh-worker-credentials.yml`：每周一 03:17Z + `workflow_dispatch`
    （可选 mode / dry_run）；从 DB 取 `enabled` 集群清单（不写死名单），逐个
    `rotate_worker_credential.py --apply`，最后跑一次 `--report-only` 报寿命。
  - `tests/test_credential_refresh_ratio.py` 8 个用例。
  **解析器修掉一个真 bug**：第一版只看 hour 字段，把 `17 3 * * 1`（每周一）读成每 24h，
  倍数因而显示 30 而不是 4.3 —— 一个把闸门悄悄放宽 7 倍的错误。现在日/周字段显式判定，
  `1-5` / `1,3,5` / 月内某日 这类"间隔不固定"的形态一律返回 None（rc=2，不猜）。
  反向验证：把 `judge()` 的 `ok` 写成恒 True → `test_verdict_rejects_a_too_long_interval` 真红。

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

- [x] 5.1 `clusters.enabled=t` 的集群全部派发一次成功——验证：故意触发一轮派发，`launched` 非空且对应 Job 在该 worker 集群上 `Complete`
  不必等窗口，**轮换之后生产自己派发过**：SA 重建于 `2026-11-11T05:33:19Z`（`kubectl get sa crawl-worker -o jsonpath={.metadata.creationTimestamp}`），
  之后 `policy_runs` 2351（p3）`status=success`、`cluster_id=9`（cheap）、`job_ref=cheap/crawl-policy-3-20261111065208`（06:52:04Z 起跑），
  该 Job 在 cheap 上 `succeeded=1`。即"读新 token → 拼 kubeconfig → 建 Job → 跑完 → 回写 run"整条链在新凭据下通。
  另有 2352（p5）仍在跑，`job_ref` 同样非空——不是"没活可派"的空绿。
  另用 `ClusterK8sClient`（与 reconciler 同一份代码）单独打了一轮探针：create Job → `job_status=success` → pod 日志 `CRED_PROBE_OK` → delete。
  该探针**严格校验证书**（`ssl.create_default_context(cafile=<cluster>.ca)`），所以同时证明 `cheap.ca` 是对的。
- [x] 5.2 `enabled=f` 的集群不再被选中——验证：调度日志中不出现这些集群名
  强于"日志里没出现"：`pick_cluster()` 是 `session.query(Cluster).filter_by(enabled=True)` 的**硬过滤**（不是排序剔除），所以非启用集群连候选集都进不去。
  DB 实测：`clusters` 4 行 / `enabled` 1 行 / 候选集 = `cheap`；近 2 天 `policy_runs` 按集群聚合：`cheap(t)` 22 次，其余 0 次。

- [x] 5.3 `openspec validate rotate-worker-cluster-credentials --strict` 通过——验证：返回 valid
  每次改动后都跑过，含本次：`Change 'rotate-worker-cluster-credentials' is valid`。


## 6. 收口

- [x] 6.1 归档——验证：`openspec archive rotate-worker-cluster-credentials --yes --strict` 成功
  **2026-11-12 归档，由用户裁决推翻原先的"等到上线再归档"**：理由是没有真实客户、系统一直在动，
  等一个观察周期的价值不如一条能红的断言。原先的顾虑（把没落地的行为写进主规格）用两条措施兜住：
  ① 代码侧判据写成构造测试（`tests/test_worker_credential_attribution.py`，见 6.2 的注）；
  ② 仍未上线的部分**没有勾**（6.2 保持未完成并写明卡点），不让勾选状态替现实说话。
- [ ] 6.2 把 3.1 的代码送上线——验证：线上镜像里 `WorkerCredentialError` 存在，
  且用伪造 token 触发一次派发能在日志里看到轮换命令

  **代码侧已完成并覆盖（2026-11-12）**：`WorkerCredentialError` / `worker_credential_hint` 已在 main
  （合并 `9678e95`），PyPI 0.5.62 内含。新增 `tests/test_worker_credential_attribution.py` 断言
  401 → 该异常且消息里带可直接执行的
  `python3 scripts/rotate_worker_credential.py --cluster cheap --apply`；403 → 只说 RBAC/禁用、
  **不**提轮换（403 是凭据有效但没权限，让人重铸凭据是往错方向指）；500 → 原样抛 `HTTPError`
  （转换必须窄）。两条注入都验过能红，还原后 14 条全绿。

  **线上侧已成立的部分**：`crawl-watcher-scan` 跑 `fd-open-data-mcp:sha-9678e95`，pod 内实测
  `__version__ 0.5.62` + `visibility/credentials.py` 存在，每 15 分钟产出 `credentials_checked: true`。

  **仍未完成的部分（本条保持未勾的原因）**：`crawl-reconciler` 与 `coverage-expander` 仍跑
  `scraw-fd-open-data-mcp:sha-daec768` = 0.5.61（pod 内实测：无 `visibility/credentials.py`，
  `import WorkerCredentialError` 报 ImportError）。**不能把它们换成本仓镜像**：2026-11-12 实测
  `fd-open-data-mcp:sha-9678e95` 里既没有 `/app/scripts` 也没有 `bulk_ingest_*.py`，而
  `_read_script()` 正是从镜像里取这些脚本拼 ConfigMap —— 换过去会打断派发。
  解锁办法仍是原链路：重建 scraw 镜像并把 `FD_ODM_INSTALL` 下限抬到 0.5.62，再改这两个 CronJob 的 image。
  判据只认镜像内实测：`python -c "import fd_open_data_mcp; print(fd_open_data_mcp.__version__)"`
  + `grep -c WorkerCredentialError`（前者 0.5.62、后者非零），不看 tag 名、不看 CI 绿灯。


## 7. 验证 5.1 时撞出来的相邻缺陷（不在本 change 的需求范围内）

不是本 change 引入的，也不是 bound/legacy 能解决的；记在这里是因为它们都在
"派发这条路"上，而 5.1 的验证必须走这条路，不记就会丢。

### 7.1 已修：`cancel` 只改库不停 pod（RBAC 缺 `delete jobs`）

- 现象：用真实 `ClusterK8sClient` 建 Job 后调 `delete_job` → `HTTP 403 Forbidden`。
- 根因：`ClusterRole/crawl-worker` 只授了 `create,get,list`（`fd-proxy-service/k8s/crawl-worker-sa.yaml`，
  注释还写着"needs exactly … Nothing else"），而 `runs.py:79` 取消时确实调 `launcher.delete(job_ref)`。
- 为什么没被发现：`runs.py:80` 把异常吞了只记日志，返回 `job_deleted=false`，行状态照样是 `cancelled`
  ——面板看起来完全正常。`ttlSecondsAfterFinished` 只回收**已结束**的 Job，所以被取消的那个会继续爬到
  `activeDeadlineSeconds`，期间继续写 master 库。
- 修法：给该 ClusterRole 的 jobs 加 `delete`（`fd-proxy-service` 提交 `4d123ef`，已推 origin/gitee，
  两个远端 sha 一致）；线上 `cheap` 用 `kubectl patch clusterrole --type=json` 落并读回。
  扩大面可忽略：`create jobs` 本来就能跑任意容器。
- 验证：同一探针 create → `success` → 日志 `CRED_PROBE_OK` → `delete_job=True` → 读回 `404`。
- 留下的后续（未做）：这份是 ClusterRole+ClusterRoleBinding（全集群生效），而 reconciler 只在
  `clusters.namespace`（scraw）动作，改成命名空间级 Role 能收窄爆炸半径。

### 7.2 未修，建议单开一个 change：读超时会留下"没人认领的 Job"

- 证据（本机 UTC，`clusters` 全部时间戳都是 UTC）：
  - `policy_runs` 2347（p2487）`status=failed`、`cluster_id=NULL`、`job_ref=NULL`，
    `detail='launch failed: The read operation timed out'`，起于 `06:37:04Z`。
  - 同一策略的 Job `crawl-policy-2487-20261111063707` 在 cheap 上 `succeeded=1`（`startTime 06:37:54Z`）。
    Job 名字里的 `063707` 是构造清单的时刻 ⇒ **POST 已经把 Job 建出来了**，客户端等响应等到
    30s 读超时才放弃。
- 对照：同一轮里 2350（p2）是 `detail='launch failed: <urlopen error _ssl.c:994: the handshake operation timed out>'`，
  那是连接都没建立，**确实没执行**。两种超时的语义完全不同，代码却都落成"launch failed + job_ref=NULL"。
- 后果：数据其实落了库，run 却记 `failed` 且没有 `job_ref`；下一周期同窗口会再跑一遍（重复抓取/覆盖写），
  而那个 Job 在面板上不可见、不可取消。
- 建议做法（不要照抄，先设计）：`launch` 的超时分支里按 `<cluster>/<jobname>` 回合核对一次
  Job 是否已存在，存在就把 `job_ref` 补上并标 `running`；只有握手类失败才可安全判 failed。
  这件事需要一个新 change（例如 `reconcile-lost-response-launch`），本 change 不夹带。
  **已另开并做完**：`openspec/changes/reconcile-lost-launch-response`（分支
  `spec/reconcile-lost-launch-response`，`1328905`）；未上线，卡在公共 PyPI 发版。

### 7.3 已修：同一份清单里写着两个镜像版本，且线上对象没有任何清单

- 现象：`fd-master/crawl-reconciler` 的容器是 `scraw-fd-open-data-mcp:sha-5539dc5`，
  而同一份 env 里 `SCRAW_K8S_IMAGE=…:sha-2302fbe`。`sha-2302fbe` 恰好是**已禁用**集群
  （america/baidu）的镜像——舰队换成 cheap 之后这条 env 没跟着改。
- 今天为什么不炸：`RECONCILER_LAUNCHER=k8s-multi` 走 `MultiClusterLauncher`，Job 镜像取
  `clusters.image`（cheap=`sha-5539dc5`），根本不构造 `K8sJobLauncher`，所以那条 env 是惰性的。
  地雷在"哪天有人切回 `RECONCILER_LAUNCHER=k8s`"——worker Job 会用上一个比集群里其它东西都旧的镜像。
- 修法：`kubectl set env cronjob/crawl-reconciler` 改成 `sha-5539dc5`，读回三处一致
  （容器 / env / `clusters.image`）。**只动 env**；我另外用 `last-applied-configuration` 逐字段对照过
  concurrency / schedule / suspend / restartPolicy / serviceAccount / imagePullSecrets / volumes /
  mounts / command / env 名单 / resources / image，均未变。
- **顺带发现的更大地雷**：这个 CronJob 在**任何仓里都没有清单**，是当年 `kubectl apply` 上去的；
  而它自己带的 `last-applied-configuration` 注解**比 live 对象少一个 `fd-scripts` ConfigMap 卷**
  （挂在 `/opt/fd-scripts`，装的是所有 `executor=direct` 策略的脚本源）。
  谁拿那份注解 apply 一次，四条 direct 策略当场全挂（`_read_script` 抛 `FileNotFoundError`）。
  已把 live 对象整理成 `k8s/fd-master/10-crawl-reconciler.yaml` 入库（忠实记录，含那句"先 diff 再 apply"）。
  另外 4 个 CronJob 同样没有真源、`last-applied-configuration` 同样过期，一并入库：
  `20-coverage-expander.yaml` / `30-crawl-watcher-scan.yaml` / `40-crawl-watcher-digest.yaml`
  / `50-fd-proxy-probe.yaml`。它们各自的注解还指着**已退役的 registry**（`harbor.local/…:latest`、
  `100.64.0.8:30880/…:sha-d1c7a78`），拿注解 apply 一次就会把镜像回滚到拉不到的地方。

### 7.4 已修：`coverage-expander` 把 `DC_API_KEY` 明文写在 env 里

- 现象：`fd-master/coverage-expander` 的 env 里 `DC_API_KEY` 是**字面值**（48 字符），
  而兄弟 CronJob `crawl-reconciler` 用的是 `secretKeyRef → dc-api-key/DC_API_KEY`。
  这把 key 一直摆在 pod spec 里，任何能 `get cronjob` / `get pod` 的人都能读到。
- 先确认是不是同一把再动手（只比指纹，不打印值）：Secret 里 `DC_API_KEY` 与列表里那个字面值
  **len 48、sha256 前 12 位 `8f23570c25fd`，完全相同** ⇒ 纯重复，不是两把不同的 key。
- 修法：`kubectl patch --type=json` 把 env[0] 换成 `valueFrom.secretKeyRef{name: dc-api-key,
  key: DC_API_KEY}`。读回：字面值消失，其余 16 条 env / 镜像 / 卷 / schedule / concurrency 均未变。
- 验证：`coverage-expander` 是 10 分钟一跳（`3,13,23,33,43,53`）。等 `11:03Z` 那一跳落地，
  `lastSuccessfulTime=11:03:06Z`、pod `Completed` ⇒ Secret 引用解析成功
  （解析不了会是 `CreateContainerConfigError`，不是 Completed）。
- **未做：轮换这把 key**。我无法在 datacommons 侧铸新 key。定级：它曾以明文存在于集群对象里，
  读取门槛是"有 fd-master 读权限"，比公开仓低得多；但本 change 就是讲凭据卫生，
  建议由持有该账号的人换一把——**换时只改 `dc-api-key` Secret 一处**，两个消费者现在都走引用。
- 附带一句实话：我为了比对把它打印进过本会话的工具输出。推之前已对入库清单做明文体检
  （`grep -E "value: [A-Za-z0-9+/=_-]{24,}"`），`k8s/fd-master/` 下无任何明文长值。

### 7.5 已修：启动器默认 DSN 里带口令，且把它当字面值写进 Job spec

- 现象（两条，同一个 launcher）：
  1. `K8sJobLauncher` / `MultiClusterLauncher` 的 `database_url` 默认值里**硬编码了
     `postgres:<口令>@fd-open-pg.scraw`**，而这个模块随 sdist 发到**公共 PyPI** —— 等于把口令公布。
  2. `K8sJobLauncher.launch()` 把 `self.database_url` 当**字面 env 值**写进 Job spec
     （`{"name": "FD_OPEN_DATA_MCP_DATABASE_URL", "value": self.database_url}`），
     而多集群 launcher 的 `_common_env` 早已改成 `secretKeyRef → fd-db-secret`。
     生产走 `k8s-multi` 所以没暴露；一旦有人切回 `RECONCILER_LAUNCHER=k8s`，
     真实 DSN（含口令）就会落进每个 pod spec，任何能 `get pods` 的人都能读。
- 为什么这条该管：仓库里的 ingest 脚本早就改成了"没有 env 就拒绝并说明"，注释还留着
  "used to fall back to a dev DSN …every Job died on connection-refused instead of saying why"。
  启动器这两处没跟上——同一类问题，只修了脚本那一半。
- 修法：两处默认值去掉凭据（`SCRAW_K8S_DATABASE_URL` 未设时为 `None`，不再编一份 DSN）；
  新增 `K8sJobLauncher._db_env()`：**有显式覆盖**才写字面值（本地开发路径，那里没有 Secret），
  **没覆盖**就走 `secretKeyRef → fd-db-secret`，与 `_common_env` 完全一致。
- 验证：`tests/test_launcher_credential_defaults.py` 7 个用例；**反向验证两遍**——
  把字面量写回调用点 → `test_launch_does_not_inline_the_dsn_any_more` 真红；
  把口令塞回默认值 → `test_no_password_in_the_default_dsn` + `test_k8s_launcher_gets_the_dsn_from_the_secret` 真红。
  相关套件 `test_reconciler / test_multi_cluster / test_direct_executor` + 新文件 → **59 passed**。
  `grep -c admin123 fd_open_data_mcp/refresh/reconciler.py` → 0（连注释里也不留这个字面值）。
- 生效条件：随下一次发版（见 6.2）。今天生产不受影响，因为两处默认值在生产都被 env 覆盖。
- 未做：`MultiClusterLauncher` 那个默认 redis URL 还带着 `:5432/postgres`（明显是复制粘贴错，
  应是 6379）。它现在也被去掉了默认值，所以这条错值一并不再存在；但若有人显式传错仍会照用。

### 7.6 已修（只加告警）：`fd-scripts` ConfigMap 是 8 月快照，与镜像里的脚本不是同一份

- 实测比对（2026-11-11，`fd-master/fd-scripts` vs 仓库 `scripts/`）：

  | key | ConfigMap | 仓库 | 判定 |
  |---|---|---|---|
  | `bulk_ingest_astock_daily.py` | 46382 | 46392 | 不一致 |
  | `bulk_ingest_financials_aggregate.py` | 14532 | 14969 | 不一致 |
  | `bulk_ingest_fund_nav_daily.py` | 11675 | 12060 | 不一致 |
  | `bulk_ingest_stock_financials.py` | 15857 | 16294 | 不一致 |
  | `bulk_ingest_stock_indicators_events.py` | 15540 | 15925 | 不一致 |
  | `run_astock_backfill_batches.py` | 10420 | 10420 | 一致（且**不在 sdist 里**，只有 ConfigMap 有） |

  另有三个 `.txt`（`cn_trade_cal_2026.txt` / `new-series-symbols.txt` /
  `universe-all-symbols.txt`）只在 ConfigMap 里，是 direct 策略真正依赖的数据文件。
- 当前**没有跑错代码**：`_read_script` 的查找顺序是 `/app/scripts` → `/opt/fd-scripts`，
  而 0.5.61 的 sdist 里 5 个 `bulk_ingest_*.py` **全都有**（从 PyPI 下 sdist 逐个核对过），
  所以镜像里那份新的赢，ConfigMap 里的旧副本被遮住。
- 真正的风险是"被遮住这件事没有任何记录"：谁改了脚本只更新 ConfigMap（或只改仓库不发版），
  都不会有任何反馈。所以 `_read_script` 现在会在**同名副本内容不同**时点名告警
  （用哪份、哪份被遮住、别两边各改一处），行为不变、只加一行 warning。
- 验证：`tests/test_read_script_shadowing.py` 3 个用例（一致时静默 / 不一致时点名 /
  找不到仍抛）；反向验证把告警改成 `if False` → 点名那条真红；
  相关套件 53 passed。
- **未做：清理 ConfigMap**。删掉那 5 个被遮住的旧 `.py` 能去掉"两份真理"，但它同时会把
  "镜像里没有就退回 ConfigMap"这条兜底变成唯一路径——今天没有收益，不值得动生产。
  要动的话前提是：先确认 sdist 永远包含全部 direct 策略脚本（有测试最好）。

  刷新执行体（`.github/workflows/refresh-worker-credentials.yml`）的落地前提，**两处自查后改掉的错**：
  1. 我第一版从 `deploy/crawl-control` 里 exec 去查 DB —— 而 `fd-master` **一个 deployment 都没有**
     （`kubectl get deploy` → No resources found）。那一步永远跑不起来。改成从控制面 Secret 的
     key（`<name>.token`）取集群清单：那本来就是控制面真实的凭据面，且不需要 DB
     （CI 连不到 tailnet 里的 PG）。"挂载 == enabled" 仍由巡检脚本在有人有 DB 时核对。
  2. `cheap` 的 API 是 `https://100.64.0.13:6443`（tailnet），**GitHub 托管 runner 连不上**。
     所以 `runs-on` 改成 `[self-hosted, linux, finddata]`，并在 preflight 里逐个 context
     `get ns`，连不上就点名失败——否则会在"刷了一半"的地方留下部分集群新、部分旧。
  3. preflight 还硬检查 `secrets.WORKER_ADMIN_KUBECONFIGS`：**缺失就直接停，什么都不写**。
     这把 admin kubeconfig 我**没有**替用户上传——那是在第三方 secret store 里新建一个
     集群管理级凭据，属于安全姿态决定，超出"放手做"的范围。所以现在这个工作流是**已接好线
     但还没通电**：缺 secret 时它会明确报错，而不会静默。
     过渡期的实际刷新方式：手动跑 `rotate_worker_credential.py --cluster cheap --mode bound --apply`
     （本机已有 `cheap-ts` admin context），或先把 secret 配好。

### 3.2 的落地版：到期前主动告警（2026-11-11 补做）

3.2 的巡检脚本能算出"剩余 < 寿命 1/3"，但**没人会每天手动跑**。而 2.1 已把 cheap 换成
720h 到期的 bound token——如果刷新没在跑，到期那天派发会**静默** 401。这条线补上了：

- `scripts/alert_stale_credentials.py`：读挂载的凭据，寿命判定 **import**
  `check_worker_credential_surface.credential_lifetime`（不重写一份），投递走已有的
  `visibility.notifiers.get_notifier()`（缺 token 只记日志、不抛）。健康时退出 0 且不推送；
  有风险退出 1 并推送，正文里带可直接执行的刷新命令。
- `k8s/fd-master/60-credential-expiry-alert.yaml`：每日 01:00 UTC 的 CronJob，
  挂 `worker-kubeconfigs` + `credential-alert-scripts`，复用 `crawl-watcher-secret`
  里现成的 ServerChan 凭据（不新造凭据、不新造 secret）。
- 验证：6 个用例（健康静默 / stale 点名 1/3 且给命令 / 过期 / legacy 不算到期风险 /
  目录缺失 rc=2 / 凭据不可解不算健康）；反向验证把"健康"改成返回 1 → 三个用例真红。
  线上真跑：`worker-credential-expiry` 的 manual Job → `Completed`、日志
  `cheap: bound 寿命 30.0d，剩余 30.0d` / `OK 没有到期风险`、`succeeded=1`。
  另用一个一次性 pod 确认投递通道已配置（`ServerChanNotifier token_len 34`），**没有真的发消息**。
- 口径提醒：这个 Job 只**告警**，从不刷新——刷新需要 `create serviceaccounts/token`，
  控制面凭据故意没有（见 `.github/workflows/refresh-worker-credentials.yml` 头部）。

### 3.2 的递归问题（已闭环：检查折进 scan，独立告警转为过渡）

到期告警原先是一条独立 CronJob（`fd-master/worker-credential-expiry`），而它**自己也无人监视**：

- `ops-console` 的 `WATCH_CRONJOBS` 只读 **cheap-ts 的 `fd-prod`** 命名空间
  （`WATCH_NAMESPACE` 未设 → 默认 `fd-prod`，取 `/apis/batch/v1/namespaces/fd-prod/cronjobs`），
  而这条 CronJob 在 **xinru 的 `fd-master`**。所以**不能**把它加进 `WATCH_CRONJOBS`——
  加了它只会永远显示 `missing: true`，变成一条永久假红（比不监视更坏）。

也就是说它是一条**没人看的 dead-man switch**：告警自己到期时同样悄无声息。
把它留在那儿只是把问题往旁边挪一格，所以按 ① 方案闭环了（方案 ②「另加一个被监视的载体」
只会再造一条同型载体，同样的递归）：

- **判定搬进包**：`fd_open_data_mcp/visibility/credentials.py`。
  `stale = 剩余 < 寿命 1/3` 这条阈值**只此一处**（`lifetime_problem_for`），
  巡检脚本与 scan 都调它——原先脚本与告警各写一份，正是本 change 一路在批的双真理。
- **检查折进 `visibility.scan`**：每 15 分钟一跳，复用已有的去重集合与微信通道，
  与失败/停滞**合并成一条消息**（不额外消耗 ServerChan 额度）。
- 三个刻意选择：目录未挂载 → `credentials_checked=false` 静默跳过（未挂载是部署形态，不是风险）；
  不可解 token → 不判定（不知道 ≠ 违规）；legacy（无 exp）→ 不告警（是选择不是故障）。
- 去重窗口单独设 **24h**，不是失败告警的 7 天：失败是一次性事件，而"凭据正在滑向过期"
  是持续为真的条件，7 天静默对一个 10 天跑道太长。

**独立 CronJob 暂留，上线后退役**（写在 `scripts/alert_stale_credentials.py` 头部）：
新代码要到 0.5.62 才进镜像，在那之前删掉它就是拆掉已有的安全网。0.5.62 部署并确认
`crawl-watcher-scan` 的 `credentials_checked` 为真之后，按顺序退役：

```bash
kubectl --context xinru -n fd-master delete cronjob worker-credential-expiry
# 然后删 k8s/fd-master/60-credential-expiry-alert.yaml、scripts/alert_stale_credentials.py
# 与 ConfigMap credential-alert-scripts；check_worker_credential_surface.py 仍保留（离线巡检用）
```

仍存的残留（诚实记下）：scan **自身**失败时依旧没人看（递归的最后一格无法靠自己消掉）。
这一格靠不住 scan，只能靠外部：`failedJobsHistoryLimit: 3` 留了 Job 记录，需要人去查。
真正的解是跨集群监视（把 xinru 侧的 CronJob 状态汇到 cheap-ts 的 ops-console），那是一条**独立
的基础设施改动**，不属于本 change。

### 上线路径已演练过（发版那天是机械操作，不是探索）

在 `origin/main`（`47b8d74`）上依次合入两条分支做了 dry-run：

- `spec/worker-cluster-credential-lifecycle` → **无冲突**
- `spec/reconcile-lost-launch-response` → **无冲突**（自动合并了 `reconciler.py`）

版本号现值 `fd_open_data_mcp/__init__.py: __version__ = "0.5.61"`，与线上镜像一致。
所以发版只剩三步：合两条分支 → 把 `__version__` 抬到 `0.5.62` → 推 `v0.5.62` tag。

复验记录（2026-11-11 晚，折进 scan 的两个提交之后）：两条分支已在 **GitHub 与 gitee 双推**
（`spec/worker-cluster-credential-lifecycle` = `ff55e2c`，`spec/reconcile-lost-launch-response` = `1328905`），
在 `origin/main`（`47b8d74`）上依次 `merge --no-commit` 仍**零冲突**，合并后的树上
`test_scan_credential_expiry` + `test_scripts_resolve_repo_package` 12 条全绿，`__version__` 仍 `0.5.61`。

> 复验方式本身踩过一个坑，记下来免得再犯：我先把 `tmp/cred-fold` 直推到
> `refs/heads/spec/worker-cluster-credential-lifecycle`，**本地同名分支没跟着动**
> （仍停在 `1844dc2`）。于是随后在本地做的"合并演练"合的是旧代码，还报"零冲突"——
> 那是**假绿**。判据不能只看"合并成功"，要看合并出来的文件清单里是否真有新文件。
> 修法：`git branch -f <branch> <sha>` 之后再演练，或始终从远端 ref 演练。

**仍未做，且不该由我单方面做**：`origin/main` 上已含**另一个会话**在办的留存契约改动
（`47b8d74` 等）。从它出发布线，会把别人在途的特性一并发到公共 PyPI，而 PyPI 版本号不可复用。
需要用户明确一句：是把 main 当前状态作为 0.5.62 发出去，还是等那条线收口。
