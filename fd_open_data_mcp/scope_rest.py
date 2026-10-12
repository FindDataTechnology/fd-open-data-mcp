"""REST JSON 面包装既有 scoping 服务（scope-rest-api 任务 1-3）。

wire（柏讯）按冻结契约（fd-wire ``api/fd_wire_api/delivery/scope_client.py``
docstring——只读参考，不改那个仓）经显式 HTTP 调本服务完成 scope 的创建/
列举/更新/删除与指标 publish/unpublish 治理；此前本服务只有 MCP 工具与
panel HTML，外部系统不跑 MCP 会话就无法触达这套面。本模块是一个轻量
FastAPI 子应用，挂载在 composite ASGI 的 ``/scopes`` 与 ``/indicators``
前缀下（server.py ``_http_asgi``），纯包装 ``fd_open_data_mcp.scoping``
既有服务——REST 层零新业务规则：四维 allow-list 校验、空 scope 拒绝、
``unscoped`` 保留名、重名拒绝全部由 ``scoping.normalize_rules`` /
``validate_rules`` / CRUD 函数裁决，这里只做 HTTP 形状映射。

契约对齐（端点形状逐字对齐 wire 客户端的调用）：

- ``POST   /scopes``                body ``{name, description?, rules}``
- ``GET    /scopes``                → scope 列表（JSON 数组）
- ``PATCH  /scopes/{name}``         body ``{rules}``
- ``DELETE /scopes/{name}``
- ``POST   /indicators/publish`` / ``/indicators/unpublish``
  body ``{indicator_codes: [str]}`` → ``scoping.set_indicators_verified``

错误载荷对齐 MCP 工具的 ``_scope_error`` 形状（server.py:1139）：
``ValueError``（校验拒：空 scope/保留名/重名）→ 400
``{"error": "invalid_scope", "detail": ...}``；``ScopeNotFound`` → 404
``{"error": "unknown_scope", "detail": ...}``——绝不静默吞服务端错误。

鉴权（全部端点过同一依赖，无会话语义、不占 MCP 会话上限）：每个请求
必须携带 ``Authorization: Bearer <SCOPE_API_TOKEN>``（env 可重指，恒时
比较对齐 server.BearerAuthMiddleware 的做法）。``SCOPE_API_TOKEN`` 未
配置时整个面诚实禁用——503 ``scope_api_disabled``，且在拿到 DB 会话之
前就拒（鉴权依赖是会话依赖的前置依赖，零副作用）；令牌不符 → 401
``unauthorized``。诚实禁用优于无鉴权裸奔：scope 是精确度/成本工具而
非权限墙，但治理写面（publish 翻公开旗）绝不能匿名可达。
"""
from __future__ import annotations

import hmac
import os

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fd_open_data_mcp import scoping
from fd_open_data_mcp.db import get_database

SCOPE_API_TOKEN_ENV = "SCOPE_API_TOKEN"


class _ScopeRestError(Exception):
    """鉴权层错误（503 诚实禁用 / 401 令牌不符）的携带体。

    不用 HTTPException：其 detail 会被包成 ``{"detail": {...}}``，而契约
    要求顶层 ``{"error", "detail"}`` 形状。
    """

    def __init__(self, status_code: int, payload: dict) -> None:
        super().__init__(payload.get("error", "scope_rest_error"))
        self.status_code = status_code
        self.payload = payload


app = FastAPI(
    title="fd-open-data-mcp scope REST",
    description=(
        "Scope CRUD + registry publish/unpublish 治理面（wire 冻结契约）。"
        "Bearer 鉴权：Authorization: Bearer $SCOPE_API_TOKEN。"
    ),
)


@app.exception_handler(_ScopeRestError)
async def _scope_rest_error_handler(request: Request,
                                    exc: _ScopeRestError) -> JSONResponse:
    return JSONResponse(exc.payload, status_code=exc.status_code)


# ─── 鉴权 + DB 会话（鉴权先行，零副作用拒）─────────────────────────────────

def _require_scope_api_token(
    authorization: str | None = Header(default=None),
) -> None:
    """Bearer 门禁：未配置 token → 503 诚实禁用；令牌不符 → 401。

    恒时比较（hmac.compare_digest）防时序侧信道，做法对齐 server.py 的
    BearerAuthMiddleware。token 每请求重读 env——运维改旗无需重启。
    """
    token = os.environ.get(SCOPE_API_TOKEN_ENV, "").strip()
    if not token:
        # 诚实禁用：不装作可用，也不匿名裸奔——治理写面必须显式配置。
        raise _ScopeRestError(503, {
            "error": "scope_api_disabled",
            "detail": "SCOPE_API_TOKEN 未配置——REST scope 面诚实禁用",
        })
    provided = authorization or ""
    if not hmac.compare_digest(provided, f"Bearer {token}"):
        raise _ScopeRestError(401, {
            "error": "unauthorized",
            "detail": "bearer 令牌不符",
        })


def _scope_api_session(
    _auth: None = Depends(_require_scope_api_token),
) -> Session:
    """鉴权作为会话依赖的前置依赖：503/401 都在打开 DB 会话之前拒。

    会话工厂与 panel/MCP 同款（``get_database().get_session()``），
    请求结束即还。
    """
    session = get_database().get_session()
    try:
        yield session
    finally:
        session.close()


# ─── 错误映射（对齐 _scope_error 载荷形状）─────────────────────────────────

@app.exception_handler(scoping.ScopeNotFound)
async def _unknown_scope_handler(request: Request,
                                 exc: scoping.ScopeNotFound) -> JSONResponse:
    return JSONResponse(
        {"error": "unknown_scope", "detail": str(exc)}, status_code=404)


@app.exception_handler(ValueError)
async def _invalid_scope_handler(request: Request,
                                 exc: ValueError) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_scope", "detail": str(exc)}, status_code=400)


# ─── 请求体模型（契约字段，逐字对齐 wire 客户端发出的 body）────────────────

class ScopeCreateBody(BaseModel):
    name: str
    rules: dict
    description: str | None = None


class ScopeUpdateBody(BaseModel):
    rules: dict


class IndicatorCodesBody(BaseModel):
    indicator_codes: list[str]


# ─── 端点（纯包装 scoping，REST 层零新业务规则）────────────────────────────

@app.post("/scopes")
def create_scope(body: ScopeCreateBody,
                 session: Session = Depends(_scope_api_session)) -> dict:
    """建 scope：scoping.scope_create（空 scope/保留名/重名 → 400）。"""
    return scoping.scope_create(session, body.name, body.rules,
                                body.description)


@app.get("/scopes")
def list_scopes(session: Session = Depends(_scope_api_session)) -> list[dict]:
    """列 scope：scoping.scope_list（含 rules 的管理面）。"""
    return scoping.scope_list(session)


@app.patch("/scopes/{name}")
def update_scope(name: str, body: ScopeUpdateBody,
                 session: Session = Depends(_scope_api_session)) -> dict:
    """改 rules：scoping.scope_update（scope 不存在 → 404）。"""
    return scoping.scope_update(session, name, body.rules)


@app.delete("/scopes/{name}")
def delete_scope(name: str,
                 session: Session = Depends(_scope_api_session)) -> dict:
    """删 scope（连带调用方默认绑定）：scoping.scope_delete。"""
    return scoping.scope_delete(session, name)


@app.post("/indicators/publish")
def publish_indicators(
    body: IndicatorCodesBody,
    session: Session = Depends(_scope_api_session),
) -> dict:
    """指标转公开（registry verified 审核旗语义）。"""
    return scoping.set_indicators_verified(
        session, body.indicator_codes, verified=True)


@app.post("/indicators/unpublish")
def unpublish_indicators(
    body: IndicatorCodesBody,
    session: Session = Depends(_scope_api_session),
) -> dict:
    """撤回公开授权，回归私有（verified=false）。"""
    return scoping.set_indicators_verified(
        session, body.indicator_codes, verified=False)
