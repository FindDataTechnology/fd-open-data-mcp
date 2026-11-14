# astock-daily-increment 运维手册

A 股全市场日线增量。**policy id 2659**，`executor=direct`，cron `17 18 * * 1-5`（CST），
脚本 `bulk_ingest_astock_daily.py --universe --slice-all 8 --slice-sleep 20`，
5203 个代码、8 个串行分片（`--slice-all` 是刻意的：4,576 符号的并发突发在 2026-11-08
冻过 xinru3 分片，不要改成并行）。

## 2026-11-12 事故：连续三天失败，而线上没有任何可用证据

`policy_runs` 连续三天只有一句：

```
job cheap/crawl-policy-2659-… failed before the runner checked in;
pod 已回收，无终态可抓 (pod reclaimed, no terminal state)
```

两处原因叠着：

1. **分片库不可用**：`xinru3`（100.64.0.7）宿主机 `/` 100% 满 → PG 写不进 WAL →
   反复 crash 进 recovery → 张量表 `ft_sem_obs_xinru3` 连不上，
   报 `FATAL: the database system is in recovery mode`。
2. **限流被硬打放大**：eastmoney 对单 IP 限流后，旧 `fetch_frame` 的三次重试之间
   **零等待**——实测 `000525 / 000608 / 000609 / 000610` 各在一秒内打完 3 次，
   然后立刻打下一个符号。akshare 的报错文案自己写着"反复触发后可能需要 40 分钟
   以上，等待期间不要重复调用"，所以硬打正是把 5 分钟封禁拖长的原因。

### 为什么证据会消失（重要）

`_direct_manifests` 建的是 `restartPolicy: OnFailure, backoffLimit: 1`。重试用尽后
Job 控制器会删掉 pod，`MultiClusterLauncher.diagnose()` 再去 `pods_for_job` 就是空，
只能记"pod 已回收"。**要看现场，必须让 pod 活下来**：

```bash
# 从现有 Job 派生一个不删 pod、不设 TTL 的调试副本
kubectl --context <cluster> -n scraw get job <job> -o json | python3 -c "
import json,sys
d=json.load(sys.stdin)
d['metadata']={'name':'<name>-debug','namespace':'scraw','labels':{'app':'debug'}}
d['spec'].pop('selector',None); d['spec'].pop('ttlSecondsAfterFinished',None)
d['spec']['backoffLimit']=0
d['spec']['template']['metadata']={'labels':{'app':'debug'}}
d['spec']['template']['spec']['restartPolicy']='Never'
print(json.dumps(d))" | kubectl --context <cluster> apply -f -
```

这样能读到 `containerStatuses[0].state.terminated.exitCode` 与完整堆栈——
本次就是靠它拿到 `psycopg2 … could not connect to server "shard_xinru3"`。

## 限流行为（2026-11-12 起）

| 常量 | 值 | 含义 |
|---|---|---|
| `RATE_LIMIT_BACKOFF_S` | 300 | 限流时重试前**必等**这么久（akshare 自述的一个窗口） |
| `RATE_LIMIT_STREAK_ABORT` | 3 | 连续 3 个符号被限流 → **中止本轮**，退出码 3 |
| `RATE_LIMIT_MARKERS` | `限流 / 断开了连接 / RemoteDisconnected / Connection aborted` | 判定标志 |

- 普通错误走 `2^n` 短退避，**不**套用 5 分钟档。
- 限流**不**记进 `failed`（它不是"该符号没数据"，记进去会伪装成数据缺口）。
- 中止时退出码 **3**（不是 0）：静默成功会让没抓到的尾巴被当成 current。
  已写入的行照常通过 `_report_yield` 上报。
- 计数**跨分片传递**：冷却 sleep 不解除封禁。

日志判据（新逻辑生效的样子）：

```
000002 hfq fetch attempt 1/3 FAILED (rate limited): … 当前 IP 被临时限流 …
waiting 300s before retry 2/3
… 第 3 个符号仍被拒 → aborting run: the upstream keeps refusing this IP
```

单测：`tests/test_astock_rate_limit_backoff.py`（5 条，注入退化实测 5/5 变红）。

## 上线一个 direct 脚本要动**三处**（否则改了不生效）

`reconciler._read_script` 的搜索顺序是 `/app/scripts`（镜像内）→ `/opt/fd-scripts`
（ConfigMap）→ `FD_SCRIPTS_DIR`，**第一个命中即用**。所以：

1. **出片** `scraw-fd-open-data-mcp`：把该仓 Dockerfile 的 `FD_ODM_REF` 抬到包含
   修复的 fd-open-data-mcp commit（必须是**完整 40 位 SHA**；构建时把上游源码树
   同时喂给 pip 和 `/app/scripts`）。
2. **抬派发器镜像**：`crawl-reconciler`（xinru/fd-master）的 `image`——
   **Job 里那份脚本是它塞进去的**。
3. **抬运行镜像**：`clusters.image` 与 cronjob 的 `SCRAW_K8S_IMAGE`
   （决定 Job 跑哪个镜像）。

判据不要看 tag 名，看内容：

```bash
kubectl --context cheap9 -n scraw get cm crawl-policy-…-script -o jsonpath='{.data}' \
 | python3 -c "import json,sys;d=json.load(sys.stdin);[print(k,len(v),'RATE_LIMIT_BACKOFF_S' in v) for k,v in d.items() if k.endswith('.py')]"
```

改 `fd-master` 的 `fd-scripts` ConfigMap **对派发出去的 Job 无效**（被镜像那份遮住），
但既然存在两份副本，仍应保持同 sha，否则每次派发都会打一条 "DIFFERENT copy exists" 告警。

## 分片依赖

`shard_xinru2`（100.64.0.6）、`shard_xinru3`（100.64.0.7），都是 39G 盘且
**备份与迁移前旧数据在同一块盘上**（2026-11-12 实测 xinru3：`/srv/fd-backup` 13G +
迁移前 PG18 卷 11G + 活库 pgdata 4G ⇒ 只剩 300M）。盘满会直接把 PG 打进 recovery。
探活在 `shard-health-alerts`（fd-prod，`*/15`），报文里带原始连接错误。
