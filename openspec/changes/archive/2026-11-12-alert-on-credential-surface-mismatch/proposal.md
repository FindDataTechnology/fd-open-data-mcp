# alert-on-credential-surface-mismatch

## Why

2026-11-12 实测：`clusters` 表里 `cheap` 早已 `enabled=false`，而控制面两个
命名空间（`fd-master`、`fd-mcp`）的 Secret `worker-kubeconfigs` 里仍躺着
`cheap.ca` + `cheap.token`——一把**可用的**、30 天寿命的凭据。它没有任何运行期
症状：派发只按 `enabled=true` 选集群，所以永远不会去用它，也就永远不会报错。

这条违规**已经有工具能判**：`scripts/check_worker_credential_surface.py` 双向断言
（不该有的不许在、该有的必须在），实测退出码 1、逐条点名。问题是**没有任何东西在
自动跑它**：`refresh-worker-credentials.yml` 需要自托管 runner（尚未配置），所以
它只存在于"有人想起来了手动跑一次"的世界里。今天这条违规是被我在排查另一件事时
顺手撞见的，而不是被告知的。

同一件事的另一面更贵：`enabled=true` 但挂载里缺 `<cluster>.token` 时，派发会在
**运行期**才 401，故障发生在真正要干活的那一刻，且日志里只表现为一次失败派发。

## What changes

把「挂载面 ↔ `clusters` 表」的一致性判定搬进包里（`visibility/credentials.py`），
让**离线巡检脚本与每 15 分钟跑一次的 `visibility.scan` 共用同一份实现**，并把
不一致折进 scan 已有的告警消息里（复用它的 24h 去重与推送通道，不额外消耗额度）。

与 `rotate-worker-cluster-credentials` 的关系：那件 change 把"到期"折进了 scan，
这件把"配错/漏配/残留"折进同一个地方。两者的判据都收在包里，`scripts/` 侧只 import。

## 刻意不做

- **不在扫描里删除/修补任何凭据**。告警只报告，处置仍走人工（或那个会话）；
  自动删一个正在被别的东西使用的凭据，比留着一个孤儿更难收场。
- **不为"未挂载"告警**。目录不存在 = 这套部署形态没有 worker 派发，属部署差异，
  与到期检查的处理保持一致（那里也是 `credentials_checked=false` 静默跳过）。
- **不新增第二条推送通道**。合并进 scan 现有的一条消息，失败/停滞/到期/面不一致
  共用一次 ServerChan 调用。
