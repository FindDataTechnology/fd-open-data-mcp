# 待裁决：worker 凭据走 bound 还是 legacy

> 一页看完就能决定。背景与完整论证见 `proposal.md` 与
> `docs/adr/0004-worker-cluster-credential-lifecycle.md`；操作步骤见
> `docs/worker-credential-runbook.md`。

## 现在的事实（都是 2026-11-11 实测，不是推演）

- 泄漏的那批 SA token 已**真正吊销**：换 SA UID，旧值 401 / 新值 200。
- 摘掉 Secret 只是"暂停"——同名重建会把同一个 JWT 原样救活。这条是本次最大的坑。
- 控制面挂载面已收敛：`fd-master` 4→2 键、`fd-mcp` 10→2 键，只剩唯一启用的 `cheap`。
- 派发能力已恢复，而且是**跑完整轮**验的：新凭据下去后生产自己派发的 `policy_runs`
  已 `success`，对应 Job 在 cheap 上 `succeeded=1`（不只是打了一次只读 API）。
- 401 现在会说人话（`WorkerCredentialError` + 轮换命令），不再藏在"launch failed"里。
- 顺手修掉一个相邻缺陷：`cancel` 以前只改数据库、pod 继续爬（RBAC 缺 `delete jobs`）。

## 要决定的事

凭据的**形态**。两种都已在 `scripts/rotate_worker_credential.py` 里实现
（`--mode bound|legacy`），所以决定的是"默认用哪个 + 要不要那套配套"，不是能不能做。

| | **bound**（`kubectl create token --duration`） | **legacy**（Secret 类 token） |
|---|---|---|
| 泄漏后 | 最长 N 天后自动失效 | 永不过期，只能换 SA UID |
| 轮换 | 改 Secret 即生效，无需重启 | 换 UID 会当场打断全部派发 |
| 新增风险 | **到期静默失效**（若配套没做） | 无（也没有任何兜底） |
| 配套要求 | 3.2 剩余寿命可见 + 3.3 刷新节奏（间隔 ≤ 寿命 1/3） | 一个手写手册 |

## 选 bound → 我接着做

1. **3.2** 就绪/健康面暴露"最近刷新时间 + 剩余寿命"
2. **3.3** 刷新 CronJob 写进清单，与 `--duration` 成 ≤1/3 关系
3. **2.1** 在窗口内跑一次 `--apply`（旧值会立即失效，需你给窗口）
4. **6.1** 归档（5.1 已用生产自己的一轮派发验完）

前提是先做 3.2/3.3 再跑 2.1——否则会把"到期静默失效"引进来却没有眼睛看着它。

## 选 legacy → 我接着做

1. 删掉 3.2 / 3.3（对 legacy 无意义）
2. 在 ADR 里把"legacy 同样会静默失败（别人删 Secret 时会 401 而 Job 看似正常）"
   写成**已知残留**，而不是当成优点
3. **2.1** 跑一次 `--apply --mode legacy`，**6.1** 同上

## 一句回答就够

回 `bound` 或 `legacy`。若选 bound，再给我一个可以打断派发的窗口（几分钟）。

## 与本次裁决无关、已可用的东西

- `scripts/check_worker_credential_surface.py` —— 双向巡检，rc=0/1/2
  （禁用却在挂载里 / 启用却缺凭据 / 孤儿 / 输入不可得）
- `scripts/rotate_worker_credential.py` 的 `--dry-run` 与 `--verify-only`（只读，已实测）
- 401 的可读错误与轮换提示

## 另有一条已记录、需单开 change（与本裁决无关）

`launch` 的**读超时**会把"Job 已建出来、数据已落库"记成 `failed` 且 `job_ref` 为空
（本机实测 2347/p2487，证据在 `tasks.md` §7.2）。后果是下一周期重复抓同一窗口，
且那个 Job 在面板上不可见、不可取消。握手超时才是"确实没执行"，两者现在被当成一件事。
