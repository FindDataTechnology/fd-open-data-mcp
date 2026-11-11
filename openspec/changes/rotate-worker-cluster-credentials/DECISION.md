# 裁决记录：worker 凭据走 bound（2026-11-11 已定并已执行）

**结论：bound。刷新在操作侧/CI 做，控制面不新增铸造权凭据。**

用户 2026-11-11 授权我自行决定 3.3 的放置，并授权执行其余项。执行结果：

- **2.1 已真跑**：`--mode bound --duration 720h --apply` 对 cheap 执行成功，
  新值可用、旧 legacy 值仍可用（bound 是加法），两个 Secret 与挂载点都已是 `bound` 剩余 30 天，
  并且用挂载点那把凭据真建了一个 Job 跑出 `BOUND_OK`。
- **3.3 已落地**：`scripts/check_credential_refresh_ratio.py` + `.github/workflows/refresh-worker-credentials.yml`；
  当前 寿命 720h / 间隔 168h = 倍数 4.3（要求 ≥ 3）。
- **保留回滚路**：没有执行 `--retire-legacy`，旧 legacy 值仍然有效；
  控制面 Secret 原值备份在 `~/finddata/secrets/sa-token-backup/before-bound-20261111T200010.json`。

以下是当初的取舍论证，留作记录。

> 背景与完整论证见 `proposal.md` 与 `docs/adr/0004-worker-cluster-credential-lifecycle.md`；
> 操作步骤见 `docs/worker-credential-runbook.md`。

## 现在的事实（都是 2026-11-11 实测，不是推演）

- 泄漏的那批 SA token 已**真正吊销**：换 SA UID，旧值 401 / 新值 200。
- 摘掉 Secret 只是"暂停"——同名重建会把同一个 JWT 原样救活。这条是本次最大的坑。
- 控制面挂载面已收敛：`fd-master` 4→2 键、`fd-mcp` 10→2 键，只剩唯一启用的 `cheap`。
- 派发能力已恢复，而且是**跑完整轮**验的：新凭据下去后生产自己派发的 `policy_runs`
  已 `success`，对应 Job 在 cheap 上 `succeeded=1`（不只是打了一次只读 API）。
- 401 的归因已经写好（`WorkerCredentialError` + 轮换命令）——**但还没上线**，见下面的"代码 vs 线上"。
- 凭据寿命现在能**看见**了：巡检脚本输出 `永不过期（legacy，无 exp）`——现状是 legacy，
  而这句话正是它的风险，以前任何看板都不显示。（脚本是运维工具，跑在哪台机器上就用哪份代码，不需要出片。）
- 顺手修掉一个相邻缺陷：`cancel` 以前只改数据库、pod 继续爬（RBAC 缺 `delete jobs`）。这条在**集群 RBAC** 上，
  已经生效，不依赖发版。

### 代码 vs 线上（2026-11-11 核实，别把"写完了"当成"生效了"）

线上 reconciler 跑的是 `ccr.ccs.tencentyun.com/finddata/scraw-fd-open-data-mcp:sha-5539dc5`，
里面 `fd_open_data_mcp.__version__ == 0.5.61`。本轮所有代码改动都在这两个分支上、**一个都没进这个版本**：

| 东西 | 状态 |
|---|---|
| 集群 RBAC 补 `delete jobs` | ✅ **已生效**（改的是集群对象，不是代码） |
| SA 重建吊销旧 token | ✅ **已生效**（旧 401 / 新 200 实测） |
| 挂载面收敛 4→2 / 10→2 键 | ✅ **已生效** |
| 401 归因（3.1） | 🟡 代码+测试已提交，**线上仍是旧包** |
| 凭据寿命巡检（3.2） | ✅ 可用（脚本，随取随跑） |
| `reconcile-lost-launch-response` | 🟡 代码+测试已提交，**线上仍是旧包** |

要上线必须走：推 `v*` tag → GitHub Actions 发 **公共 PyPI** → 重建 scraw 镜像 → 改两个 CronJob 的 image。
第一步是对外且不可逆的（PyPI 版本号不可复用），**要你点头**才能按。

## 当初要决定的事

凭据的**形态**。两种都已在 `scripts/rotate_worker_credential.py` 里实现
（`--mode bound|legacy`），所以决定的是"默认用哪个 + 要不要那套配套"，不是能不能做。

| | **bound**（`kubectl create token --duration`） | **legacy**（Secret 类 token） |
|---|---|---|
| 泄漏后 | 最长 N 天后自动失效 | 永不过期，只能换 SA UID |
| 轮换 | 改 Secret 即生效，无需重启 | 换 UID 会当场打断全部派发 |
| 新增风险 | **到期静默失效**（若配套没做） | 无（也没有任何兜底） |
| 配套要求 | 3.2 剩余寿命可见 + 3.3 刷新节奏（间隔 ≤ 寿命 1/3） | 一个手写手册 |
| 3.2 现状 | ✅ 已做（健康面能读出寿命 / 剩余 / STALE） | ✅ 同一套（输出"永不过期，无兜底"） |
| 3.3 的真实代价 | ⚠️ 见下：签 token 需要铸造权 | 无额外凭据 |

### 3.3 的卡点与解法（2026-11-11 实测 + 已定方案）

```
$ kubectl auth can-i create serviceaccounts/token \
    --as=system:serviceaccount:scraw:crawl-worker -n scraw
no
（对照：create jobs / delete jobs / get jobs → 全是 yes）
```

签 bound token 是 API server 的动作，需要铸造权，而控制面握着的正是这个"能建能删 Job、
不能签 token"的凭据。

**采用的解法：不在控制面铸造。** 给控制面加一把最小铸造权凭据（
`resources: [serviceaccounts/token]` + `resourceNames: [crawl-worker]`）技术上可行，
但那样一来**那把凭据本身也得长期有效、也得被刷新**——问题只是往上挪一层，还多一把更强的
常驻凭据。所以铸造改在集群外，用运维已有的 admin kubeconfig，只把签好的 token 写回 Secret。
控制面永远只有一把低权限凭据。

代价：刷新依赖外部定时器（`.github/workflows/refresh-worker-credentials.yml`，周一 03:17Z）。
这个代价由 3.2 的 STALE 告警兜住——剩余寿命跌破 1/3 就先亮，比 401 早一个刷新周期。

## 已执行（不再是待办）

1. ✅ **3.2** 剩余寿命可见（`check_worker_credential_surface.py`，含 `--json`）
2. ✅ **3.3** 刷新放在操作侧/CI；`check_credential_refresh_ratio.py` 断言 间隔 × 3 ≤ 寿命
   （当前 720h / 168h = 4.3）
3. ✅ **2.1** 真跑 `--apply --mode bound --duration 720h`：新值可用、旧值仍可用（加法），
   两个 Secret + 挂载点均为 bound 剩余 30d，并用挂载点凭据真建 Job 跑出 `BOUND_OK`
4. ⏳ **6.1** 归档 —— **有意延后**：3.1 等代码仍在分支上、线上仍是 0.5.61，
   此刻归档等于把未上线行为写进主规格。顺序是 6.2 上线 → 观察一周期 → 再归档。

## 回滚路（保留中）

`--retire-legacy` 没有执行，worker 上那把 legacy token Secret 仍在，旧值仍可用。
控制面 Secret 原值备份：`~/finddata/secrets/sa-token-backup/before-bound-20261111T200010.json`。
要回滚就把 `cheap.token` 换回备份里的值；要彻底收口（让旧值真正失效）则跑
`--retire-legacy`，那一步**会掐掉这条回滚路**。

## 若当初选 legacy 会怎么做（留档，未走这条）

1. **3.2 保留**（它输出的"永不过期"正是 legacy 的主要风险，删掉反而看不见了）；只删 3.3
2. 在 ADR 里把"legacy 同样会静默失败（别人删 Secret 时会 401 而 Job 看似正常）"
   写成**已知残留**，而不是当成优点
3. `--apply --mode legacy`（换 SA UID，会当场打断派发，需要窗口）

## 一句话背景：为什么不需要窗口

bound 是**加法**——签的是另一把凭据，worker 上那把 legacy token Secret 没动，
所以旧值照常可用，不存在"旧凭据立即失效"的中断。要窗口的是 legacy 那条路（换 SA UID）。

## 与本次裁决无关、已可用的东西

- `scripts/check_worker_credential_surface.py` —— 双向巡检，rc=0/1/2
  （禁用却在挂载里 / 启用却缺凭据 / 孤儿 / 输入不可得）
- `scripts/rotate_worker_credential.py` 的 `--dry-run` 与 `--verify-only`（只读，已实测）
- 401 的可读错误与轮换提示

## 另有一条已记录、需单开 change（与本裁决无关）

`launch` 的**读超时**会把"Job 已建出来、数据已落库"记成 `failed` 且 `job_ref` 为空
（本机实测 2347/p2487，证据在 `tasks.md` §7.2）。后果是下一周期重复抓同一窗口，
且那个 Job 在面板上不可见、不可取消。握手超时才是"确实没执行"，两者现在被当成一件事。
