# worker-credential-lifecycle Specification

## Purpose
Worker 集群的派发凭据是 control-plane 与 worker 之间唯一的信任凭据，它有三种失效方式，
每一种都很安静：到期（bound token 到点即 401，此前毫无征兆）、配置漂移（禁用的集群留着
可用凭据＝给自己留后门；启用的集群缺凭据＝派发时才 401）、以及"检查本身失效"（挂载没了、
密钥键改名，告警从此哑掉而外部读数仍说健康）。本能力规定这三类的**唯一判据落点**与
**告警归属**：判据集中在 `visibility/credentials.py`，由每 15 分钟的 `visibility.scan`
判定并折进它已有的那一条消息（复用去重与推送通道），离线巡检脚本调同一份实现；所有
检查都区分"没配上"与"配上了但坏了"——前者不告警，后者必须告警。

## Requirements

### Requirement: 挂载面与 `clusters` 表的一致性 SHALL 只有一个判定实现

「哪些凭据不该在挂载里、哪些必须有」这条判定 SHALL 只存在于
`fd_open_data_mcp/visibility/credentials.py`；离线巡检脚本与集群内扫描
SHALL 调用同一份实现，MUST NOT 各自复制一份字符串逻辑。

判定输入是两个集合：挂载里出现过的集群名（按 `<name>.token` 归并）与
`clusters` 表的 `{name: enabled}`。输出为逐条问题记录，三种形态：
孤儿（表里没有这个集群）、禁用却有凭据（`enabled=false` 但挂载里有）、
缺失（`enabled=true` 但挂载里没有）。

#### Scenario: 表里没有的集群仍有凭据

- **WHEN** 挂载目录里存在 `america.token`，而 `clusters` 表没有 `america` 这一行
- **THEN** 判定输出一条"孤儿凭据"，点名 `america`
- **AND** 同一条问题在离线巡检与集群内扫描里逐字相同

#### Scenario: 禁用集群仍挂着凭据

- **WHEN** 挂载目录里有 `cheap.token`，而 `clusters` 表里 `cheap.enabled = false`
- **THEN** 判定输出一条"禁用却有凭据"，点名 `cheap`
- **AND** 措辞说明正确处置是"重新启用时另签新凭据"（保留旧值等于给自己留后门）

#### Scenario: 启用集群缺凭据

- **WHEN** `clusters` 表里 `cheap9.enabled = true`，而挂载目录里没有 `cheap9.token`
- **THEN** 判定输出一条"缺失"，并说明派发会在运行期 401

### Requirement: 凭据面不一致 SHALL 由每 15 分钟的扫描自动告警

`fd_open_data_mcp.visibility.scan` SHALL 在每轮扫描中比较挂载面与 `clusters` 表，
把不一致折进它已有的那一条告警消息里（与失败/停滞/到期共用一次推送），
并按 `credential-surface:<cluster>:<kind>` 去重、静默窗口 24 小时。

挂载目录不存在时 SHALL 静默跳过（部署形态差异，不是风险），并把
`credentials_checked` 置为 `false`——与到期检查共用同一个"真的查过吗"判据。

#### Scenario: 启用集群缺凭据时在派发之前就告警

- **WHEN** `clusters` 表里某集群 `enabled = true`，而它的 `<name>.token` 不在挂载里
- **THEN** 本轮扫描产出一条告警，正文点名该集群与"派发会 401"
- **AND** 这条告警在派发真正失败之前发出（不需要等一次 401 才被发现）

#### Scenario: 一致的部署不产生任何噪音

- **WHEN** 挂载里只有 `cheap9.token`，`clusters` 表里也只有 `cheap9.enabled = true`
- **THEN** 本轮扫描不产出凭据面告警，也不发送任何消息

#### Scenario: 未挂载时不告警也不假装查过

- **WHEN** 凭据目录不存在
- **THEN** 扫描静默跳过，`credentials_checked` 为 `false`，不发送消息

#### Scenario: 持续为真的不一致按天重提，且只提一次

- **WHEN** 同一处不一致在 24 小时内连续多轮被判定为真
- **THEN** 首轮发送并记录去重键，后续轮次不再重复推送
- **AND** 超过 24 小时后再次为真时重新推送
