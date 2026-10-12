# Tasks

## 1. 服务层

- [x] 1.1 `scoping.py` 新增 `set_indicators_verified(session, codes, verified)`：UPDATE registry_entries 按 semantic_code 翻 verified；返回 `{"updated": n, "unknown": [...]}`（未知如实上报）；无 registry 表环境（dev sqlite）→ updated=0 + 全 unknown（诚实）

## 2. REST 面

- [x] 2.1 新模块 `scope_rest.py`：FastAPI 子应用五端点（POST/GET/PATCH/DELETE /scopes、POST /indicators/publish|unpublish）——包装 scoping 服务，错误形状映射（ValueError→400 invalid_scope、ScopeNotFound→404 unknown_scope）
- [x] 2.2 鉴权依赖：Bearer SCOPE_API_TOKEN（不符 401；env 未配置 503 scope_api_disabled 零 DB 副作用）
- [x] 2.3 `server.py` `_http_asgi()` composite 挂载：path 前缀 `/scopes`、`/indicators` → REST 子应用（/panel 与 /mcp 路由不变）

## 3. 测试

- [x] 3.1 `tests/test_scope_rest.py`（照 test_scope_system.py 的 sqlite+REGISTRY_DDL 种子范式）：鉴权矩阵（未配置 503/错令牌 401/对令牌 200）、CRUD 往返（建→列→改→删）、空 scope 400、不存在 404、publish/unpublish 翻旗+unknown 上报、错误形状逐键断言
- [x] 3.2 变异自证：去掉鉴权依赖 → 401 用例红；去掉 ScopeNotFound 映射 → 404 用例红；恢复全绿

## 4. 门禁与发布

- [x] 4.1 全量 pytest 绿 + `openspec validate scope-rest-api --strict`
- [x] 4.2 双推（gitee+github）→ GHA 构建 → fd-infra-deploy all-services/prod：fd-open-data-mcp 加 SCOPE_API_TOKEN secret env + 滚镜像 tag → ArgoCD 刷新 → 冒烟（对令牌 GET /scopes 200、无令牌 401、未配置路径不受影响）

## 5. 归档

- [x] 5.1 生产实弹通过后 `openspec archive scope-rest-api -y` + `validate --specs` + CHANGELOG 条目
