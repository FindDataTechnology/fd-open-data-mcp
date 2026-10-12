"""scope-rest-api 任务 1-3: REST scope 面的鉴权矩阵、CRUD 往返、错误映射
与 publish 治理——TestClient 直测 FastAPI 子应用（fd_open_data_mcp/scope_rest）。

种子范式照 test_scope_system.py：内存位 tmp sqlite + REGISTRY_DDL 建表 +
verified 0/1 行；scope 三表经 conftest 的 models create_all。REST 子应用每
请求经 get_database().get_session() 拿同一个 tmp 库——fixture 已重置单例
并指向它。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from fd_open_data_mcp import scoping, server
from fd_open_data_mcp.scope_rest import app as scope_rest_app

client = TestClient(scope_rest_app)

TOKEN = "test-scope-api-token"

REGISTRY_DDL = """
CREATE TABLE registry_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_db VARCHAR(64),
  source_table VARCHAR(128),
  source_column VARCHAR(128),
  native_code VARCHAR(128),
  semantic_code VARCHAR(128),
  name_zh VARCHAR(255),
  name_en VARCHAR(255),
  unit VARCHAR(64),
  frequency VARCHAR(32),
  domain VARCHAR(64),
  verified INTEGER DEFAULT 0
)
"""

#: publish 测试翻的旗在 verified=0 行上；其余两行照 test_scope_system 种子。
SEED_ROWS = (
    {"source_db": "yearbook_catalog", "native_code": "10401",
     "semantic_code": "yb.total_population", "domain": "人口", "verified": 1},
    {"source_db": "world_bank", "native_code": "SP.POP.TOTL",
     "semantic_code": "wb.population_total", "domain": "人口", "verified": 1},
    {"source_db": "yearbook_catalog", "native_code": "10402",
     "semantic_code": "yb.pending_indicator", "domain": "人口", "verified": 0},
)


@pytest.fixture
def seeded(session):
    session.execute(text(REGISTRY_DDL))
    for row in SEED_ROWS:
        row = {**row, "source_table": "t", "source_column": "v",
               "name_zh": "n", "name_en": "n", "unit": "u", "frequency": "yearly"}
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        session.execute(text(f"INSERT INTO registry_entries ({cols}) VALUES ({marks})"), row)
    session.commit()
    return session


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("SCOPE_API_TOKEN", TOKEN)
    return monkeypatch


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _verified(session, code: str) -> int:
    return session.execute(
        text("SELECT verified FROM registry_entries WHERE semantic_code = :c"),
        {"c": code}).scalar()


# ─── 鉴权矩阵 ────────────────────────────────────────────────────────────────

def test_token_unset_disables_face_with_zero_side_effects(seeded, monkeypatch):
    """SCOPE_API_TOKEN 未配置 → 503 scope_api_disabled，且库零变化——
    诚实禁用在进 DB 之前拒，绝不无鉴权裸奔。"""
    monkeypatch.delenv("SCOPE_API_TOKEN", raising=False)

    for method, path, body in (
        ("post", "/scopes", {"name": "x", "rules": {"source_dbs": ["yearbook_catalog"]}}),
        ("get", "/scopes", None),
        ("patch", "/scopes/x", {"rules": {"source_dbs": ["world_bank"]}}),
        ("delete", "/scopes/x", None),
        ("post", "/indicators/publish", {"indicator_codes": ["yb.pending_indicator"]}),
        ("post", "/indicators/unpublish", {"indicator_codes": ["yb.total_population"]}),
    ):
        kwargs = {"json": body} if body is not None else {}
        resp = getattr(client, method)(path, headers=_auth(), **kwargs)
        assert resp.status_code == 503, (method, path, resp.text)
        assert resp.json()["error"] == "scope_api_disabled"
        assert "诚实禁用" in resp.json()["detail"]

    assert scoping.scope_list(seeded) == []  # 零 scope 写入
    assert _verified(seeded, "yb.pending_indicator") == 0   # 审核旗未被动过
    assert _verified(seeded, "yb.total_population") == 1


def test_wrong_or_missing_token_is_401(seeded, token_env):
    """令牌不符（或缺失）→ 401 unauthorized；库同样零变化。"""
    for headers in ({"Authorization": f"Bearer wrong-{TOKEN}"}, {}):
        resp = client.post("/scopes", json={
            "name": "x", "rules": {"source_dbs": ["yearbook_catalog"]}},
            headers=headers)
        assert resp.status_code == 401, resp.text
        assert resp.json() == {"error": "unauthorized", "detail": "bearer 令牌不符"}
    assert scoping.scope_list(seeded) == []


def test_correct_token_is_served(seeded, token_env):
    resp = client.get("/scopes", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == []


# ─── CRUD 往返（对齐 wire 冻结契约的端点形状）────────────────────────────────

def test_crud_round_trip(seeded, token_env):
    created = client.post("/scopes", json={
        "name": "yearbook_only",
        "description": "yearbook indicators only",
        "rules": {"source_dbs": ["yearbook_catalog"]},
    }, headers=_auth())
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["name"] == "yearbook_only"
    assert body["matched"] >= 1  # 至少一维命中种子已知值

    listed = client.get("/scopes", headers=_auth())
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()] == ["yearbook_only"]

    patched = client.patch("/scopes/yearbook_only", json={
        "rules": {"source_dbs": ["yearbook_catalog", "world_bank"]}},
        headers=_auth())
    assert patched.status_code == 200, patched.text
    assert set(patched.json()["rules"]["source_dbs"]) == {
        "yearbook_catalog", "world_bank"}

    deleted = client.delete("/scopes/yearbook_only", headers=_auth())
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == "yearbook_only"

    assert [s["name"] for s in client.get("/scopes", headers=_auth()).json()] == []


# ─── 错误映射（对齐 _scope_error 载荷形状）───────────────────────────────────

def test_empty_scope_rejected_400(seeded, token_env):
    """四维全空（无论省略还是显式空列表）→ 400 invalid_scope。"""
    for rules in ({}, {"source_dbs": [], "domains": [],
                       "semantic_codes": [], "native_codes": []}):
        resp = client.post("/scopes", json={
            "name": "empty", "rules": rules}, headers=_auth())
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "invalid_scope"
        assert resp.json()["detail"]


def test_unknown_scope_patch_delete_404(seeded, token_env):
    for method in ("patch", "delete"):
        kwargs = ({"json": {"rules": {"source_dbs": ["yearbook_catalog"]}}}
                  if method == "patch" else {})
        resp = getattr(client, method)("/scopes/ghost", headers=_auth(), **kwargs)
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"] == "unknown_scope"
        assert "ghost" in resp.json()["detail"]


def test_duplicate_create_400(seeded, token_env):
    body = {"name": "dup", "rules": {"source_dbs": ["yearbook_catalog"]}}
    assert client.post("/scopes", json=body, headers=_auth()).status_code == 200
    again = client.post("/scopes", json=body, headers=_auth())
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_scope"
    assert "already exists" in again.json()["detail"]


# ─── publish / unpublish 治理（registry verified 审核旗）─────────────────────

def test_publish_flips_verified(seeded, token_env):
    resp = client.post("/indicators/publish",
                       json={"indicator_codes": ["yb.pending_indicator"]},
                       headers=_auth())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"updated": 1, "unknown": []}
    assert _verified(seeded, "yb.pending_indicator") == 1


def test_publish_reports_unknown_codes(seeded, token_env):
    """混入未知 code：已知照翻，unknown 如实列出。"""
    resp = client.post("/indicators/publish",
                       json={"indicator_codes": ["yb.pending_indicator", "no.such.code"]},
                       headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"updated": 1, "unknown": ["no.such.code"]}
    assert _verified(seeded, "yb.pending_indicator") == 1


def test_unpublish_flips_back(seeded, token_env):
    client.post("/indicators/publish",
                json={"indicator_codes": ["yb.pending_indicator"]}, headers=_auth())
    resp = client.post("/indicators/unpublish",
                       json={"indicator_codes": ["yb.pending_indicator"]},
                       headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"updated": 1, "unknown": []}
    assert _verified(seeded, "yb.pending_indicator") == 0


def test_publish_without_registry_table_is_honest(session, token_env):
    """dev sqlite 未建 registry_entries：updated=0 + 全 unknown，如实不炸
    （scoping.set_indicators_verified 捕获 OperationalError/ProgrammingError）。"""
    resp = client.post("/indicators/publish",
                       json={"indicator_codes": ["yb.total_population"]},
                       headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"updated": 0, "unknown": ["yb.total_population"]}
    assert scoping.set_indicators_verified(session, ["a", "b"], True) == {
        "updated": 0, "unknown": ["a", "b"]}


# ─── composite 挂载（server._http_asgi 分派）────────────────────────────────

def test_composite_routes_scope_prefix_to_rest_face(seeded, token_env):
    """/scopes 前缀必须落到 REST 子应用而非 MCP——落 MCP 会是 404，
    这里拿到 REST 面自己的 401 即证明分派正确（websocket 照旧落 MCP）。"""
    composite = server._http_asgi()
    composite_client = TestClient(composite)  # 不跑 lifespan：/scopes 请求不经 mcp_asgi
    resp = composite_client.post("/scopes", json={
        "name": "x", "rules": {"source_dbs": ["yearbook_catalog"]}})
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"
    # 对令牌经 composite 走完整 CRUD 一发（分派 + 面本身）
    ok = composite_client.post("/scopes", json={
        "name": "x", "rules": {"source_dbs": ["yearbook_catalog"]}},
        headers=_auth())
    assert ok.status_code == 200 and ok.json()["name"] == "x"
