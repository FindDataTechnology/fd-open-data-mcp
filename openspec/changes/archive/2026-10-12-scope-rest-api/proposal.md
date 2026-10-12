# scope-rest-api

## Why

wire（柏讯）的需求单交付承诺「交付时自动开客户私有 scope」——wire 侧机制完备
（`delivery/scope_client.py` 按 `SCOPE_API_BASE` 调 REST 契约，未配置时如实降级
pending），但本服务的 scope 能力只暴露为 MCP 工具（`scope_create` 等）与 panel
HTML 表单，**没有 wire 期望的 REST JSON 面**。生产实证：两张已交付需求单
scope_status 恒 pending、wire pod 无 SCOPE_API_BASE——方向 C 交付链的最后一环
（客户私有数据可读）从未真正生效。

本 change 补上这块：在既有 composite ASGI 上挂一个轻量 REST 面，五端点逐字对齐
wire `scope_client.py` 已冻结的调用形状（该文件 docstring 即契约），包装既有
`scoping.py` 服务——**零新表、零迁移、零业务逻辑**（校验/幂等/统计全复用）。

## What Changes

- **新模块 `fd_open_data_mcp/scope_rest.py`**：FastAPI 子应用，五端点：
  - `POST /scopes`（body `{name, description, rules}` 四维 allow-list）→
    `scoping.scope_create`；
  - `GET /scopes` → `scoping.scope_list`；
  - `PATCH /scopes/{name}`（body `{rules}`）→ `scoping.scope_update`；
  - `DELETE /scopes/{name}` → `scoping.scope_delete`（wire 契约未用，演练清场
    与运维需要）；
  - `POST /indicators/publish|unpublish`（body `{indicator_codes}`）→
    registry_entries 的 verified 审核旗（publish=转公共可读，unpublish=回归
    私有）——`scoping.py` 新增 `set_indicators_verified(session, codes,
    verified)` 服务函数，返回 `{updated, unknown}`（未知 code 如实上报不虚构）。
- **鉴权**：`Authorization: Bearer <SCOPE_API_TOKEN>`——env 未配置 → 全端点
  503 `{"error": "scope_api_disabled"}`（诚实禁用，不裸奔）；令牌不符 → 401
  `{"error": "unauthorized"}`。令牌仅经 k8s secret 下发（服务间内网面）。
- **错误形状**（对齐 wire 客户端冻结契约）：`ValueError`（校验拒）→ 400
  `{"error": "invalid_scope", "detail"}`；`ScopeNotFound` → 404
  `{"error": "unknown_scope", "detail"}`；绝不静默吞服务端错误。
- **挂载**：`server.py` `_http_asgi()` composite 增加路由分支——
  path 以 `/scopes` 或 `/indicators` 开头走 REST 子应用，`/panel` 照旧，其余
  落 MCP streamable-http（无冲突：MCP 端点在 `/mcp`）。REST 面无会话语义，
  不占 MCP 会话上限。
- **部署**：`fd-infra-deploy` all-services/prod 的 fd-open-data-mcp 清单加
  `SCOPE_API_TOKEN`（secret）+ 滚镜像 tag。

## Design Decisions

- **包装而非重写**：所有校验（normalize/validate/空 scope 拒）、幂等、命中
  统计留在 `scoping.py`；REST 层只做 HTTP 形状映射与鉴权，测试对拍服务层
  语义。
- **publish 语义=verified 旗**：与既有公开读面口径一致
  （`list_registry_entries WHERE verified`）；wire 侧「授权转公开」的冻结
  契约即此形状。unknown code 不拒整批（native_code 漂移是既有设计权衡），
  逐个如实上报。
- **令牌独立于 MCP_BEARER_TOKEN**：scope 面是服务间契约（wire↔本服务），
  与 MCP 客户端入口的令牌生命周期不同，独立 env 独立轮换。

## Capabilities

### New Capabilities

- `scope-rest-api`: scope/indicators 的 REST JSON 面——五端点契约、bearer
  鉴权与诚实禁用、错误形状、verified 发布语义。

## Impact

- **fd_open_data_mcp/**：`scope_rest.py`（新）、`scoping.py`（+
  `set_indicators_verified`）、`server.py`（composite 挂载分支）。
- **tests/**：`test_scope_rest.py`（鉴权矩阵/CRUD 往返/错误形状/publish 翻旗
  与 unknown 上报）。
- **fd-infra-deploy**：all-services/prod fd-open-data-mcp env + 镜像滚版。
- 零迁移、零新表。
