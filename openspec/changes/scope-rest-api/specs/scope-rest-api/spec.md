# scope-rest-api Specification

## ADDED Requirements

### Requirement: REST scope 端点

服务 SHALL 提供 REST JSON 面（挂载于既有 composite ASGI）：`POST /scopes`（建
命名 scope，body 含 name/description/rules 四维 allow-list）、`GET /scopes`
（列全部含 rules）、`PATCH /scopes/{name}`（整体回写 rules）、`DELETE
/scopes/{name}`（删除）；语义逐字复用既有 scoping 服务（校验、空 scope 拒、
幂等、命中统计），REST 层不新增业务规则。

#### Scenario: 建 scope 往返

- **WHEN** 携合法 bearer 令牌 `POST /scopes`（rules 至少一维非空且命中已知值）
- **THEN** 201/200 返回 scope 载荷，随后 `GET /scopes` 可见该 scope 与 rules

#### Scenario: 空 scope 拒

- **WHEN** POST /scopes 的 rules 四维全空
- **THEN** 400 `{"error": "invalid_scope", "detail": ...}`，不落任何行

### Requirement: 指标发布端点

服务 SHALL 提供 `POST /indicators/publish` 与 `POST /indicators/unpublish`
（body `{indicator_codes}`）：分别把 registry_entries 对应 semantic_code 的
verified 审核旗置 true/false（publish=转公共可读，unpublish=回归私有）；响应
含 `updated` 计数与 `unknown` 清单，未知 code 不拒整批、逐个如实上报。

#### Scenario: 发布翻旗

- **WHEN** 对一个 verified=false 的 registry 行 publish 其 semantic_code
- **THEN** 该行 verified=true，响应 updated=1、unknown=[]

#### Scenario: 未知 code 如实

- **WHEN** publish 混入不存在的 semantic_code
- **THEN** 已知照常翻旗，unknown 列出该 code，不报错不虚构

### Requirement: 鉴权与诚实禁用

REST 面 SHALL 以 `Authorization: Bearer <SCOPE_API_TOKEN>` 鉴权：令牌不符 401
`{"error": "unauthorized"}`；env 未配置时全部端点 503
`{"error": "scope_api_disabled", "detail": ...}`（诚实禁用，不提供未鉴权
面）。令牌仅经环境变量/secret 注入，不落代码或文档明文。

#### Scenario: 未配置禁用

- **WHEN** SCOPE_API_TOKEN 未配置时请求任一端点
- **THEN** 503 scope_api_disabled，数据库零副作用

#### Scenario: 错误令牌拒

- **WHEN** 携错误 bearer 请求
- **THEN** 401，不泄露令牌期望值

### Requirement: 错误形状契约

REST 面 SHALL 以冻结错误形状返回服务层拒绝：scoping 校验类拒绝（ValueError）
→ 400 `{"error": "invalid_scope", "detail"}`；scope 不存在（ScopeNotFound）
→ 404 `{"error": "unknown_scope", "detail"}`；服务端错误绝不静默吞掉。

#### Scenario: 不存在的 scope

- **WHEN** PATCH/DELETE 一个不存在的 scope 名
- **THEN** 404 unknown_scope + detail 含该名字
