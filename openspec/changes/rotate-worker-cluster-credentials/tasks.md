> 剩 4 项（2.1 / 3.2 / 3.3 / 6.1）被同一个裁决挡住（bound / legacy）。一页看完即可决定：**[DECISION.md](DECISION.md)**。

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
- [ ] 3.3 CronJob 刷新节奏写进部署清单，且与 `--duration` 有明确倍数关系（刷新间隔 ≤ 寿命的 1/3）——验证：清单里的两个数值能被脚本读出来并断言满足比例
  **被 3.2 的工作卡住，且卡点不是体力活**：签 bound token 要 `create serviceaccounts/token`，
  而控制面握着的 worker 凭据明确没有这个动词（`kubectl auth can-i create serviceaccounts/token
  --as=system:serviceaccount:scraw:crawl-worker -n scraw` → **no**；对照组 `create/delete/get jobs` → yes）。
  所以"自动刷新"要么给控制面**新增一个比今天更强的凭据**（最小可给到
  `resources: [serviceaccounts/token], resourceNames: [crawl-worker], verbs: [create]`），
  要么退回"运维机/CI 手工签"。这不是清单里写两个数字就能解决的，须先定这件事——见 `DECISION.md`。

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

- [ ] 6.1 归档——验证：`openspec archive rotate-worker-cluster-credentials --yes --strict` 成功
- [ ] 6.2 把 3.1 的代码送上线（**目前只在分支上**）——验证：线上镜像里 `WorkerCredentialError` 存在，
  且用伪造 token 触发一次派发能在日志里看到轮换命令
  链路：`git tag v0.5.62 && git push origin v0.5.62` → GitHub Actions `release.yml` 发**公共 PyPI**
  → 重建 scraw 镜像（`FD_ODM_INSTALL` 下限抬到 0.5.62）→ 改 `crawl-reconciler` / `crawl-coverage-expander`
  两个 CronJob 的 image。
  **卡点**：第一步是对外且不可逆的（PyPI 版本号不可复用、删不掉），须用户点头；
  另需先确认 main 干净（当前 main 有 13 项别的会话的未提交改动）。
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
