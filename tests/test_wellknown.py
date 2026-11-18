"""线自描述端点 ``/.well-known/line-manifest.json`` 的判据
（change converge-line-identity-and-keys 1.4）。

判据都对着**这个端点自己**，不拿邻近信号冒充（同族教训：`/health` 绿不等于清单在）：

1. 成功响应与仓内 ``line-manifest.json`` **逐字节相同**——端点是投影，不是第二真源；
2. ``Cache-Control`` 只在 200 上出现，别把"暂时没有"缓存成长期事实；
3. 路径在门的 ``EXEMPT_PATHS`` 里——只挂路由不加豁免，配了令牌的线仍会对匿名发现
   请求回 401（这正是六条线此前"匿名 401"的成因之一）；
4. ``register()`` 挂的就是门豁免的那个路径（同一个常量，不许各写各的）；
5. ``LINE_MANIFEST_PATH`` 生效且封顶候选链；
6. 清单缺失 = 404、存在但坏 = 500，两者不能混（404 会让调用方以为"本来就没发布"）；
7. 真装配：配了 ``MCP_BEARER_TOKEN`` 时端点匿名 200 且字节相同，``/mcp`` 仍 401。

本仓装配点没有 ``build_server()``：``mcp`` 是 ``server`` 模块里的全局 ``FastMCP``，
HTTP 面由 ``server._http_asgi()`` 组装。所以"真装配"走那条真实路径，而不是另造一个。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from fd_open_data_mcp import server, wellknown
from fd_open_data_mcp.server import EXEMPT_PATHS

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_MANIFEST = REPO_ROOT / "line-manifest.json"


# ─── 1. 原样回吐 ─────────────────────────────────────────────────────────────

def test_serves_repo_manifest_byte_for_byte():
    """端点的字节 == 仓内真源的字节（不是"解析后再 dump"）。"""
    assert REPO_MANIFEST.exists(), f"仓内真源不见了：{REPO_MANIFEST}"
    status, body, headers = wellknown.manifest_response()
    assert status == 200
    assert body == REPO_MANIFEST.read_bytes()
    assert json.loads(body)["line"]  # 至少是个带 line 的清单，不是空壳


# ─── 2. 缓存头只在 200 ──────────────────────────────────────────────────────

def test_success_carries_cache_control():
    status, _, headers = wellknown.manifest_response()
    assert status == 200
    assert headers == {"cache-control": "public, max-age=300"}


# ─── 3. 路径在门的豁免表里 ───────────────────────────────────────────────────

def test_well_known_path_is_gate_exempt():
    """路由与豁免必须成对；这里钉住后半句。"""
    assert wellknown.WELL_KNOWN_PATH in EXEMPT_PATHS


# ─── 4. register() 与豁免同一个常量 ─────────────────────────────────────────

def test_register_mounts_exactly_that_path():
    """``register()`` 挂的就是门豁免的那个路径（两处不许各写各的字面量）。"""
    pytest.importorskip("starlette")

    class _FakeMCP:
        def __init__(self):
            self.routes: list[tuple[str, object]] = []

        def custom_route(self, path, methods=None):
            def deco(fn):
                self.routes.append((path, fn))
                return fn

            return deco

    mcp = _FakeMCP()
    wellknown.register(mcp)
    assert [p for p, _ in mcp.routes] == [wellknown.WELL_KNOWN_PATH]
    assert wellknown.WELL_KNOWN_PATH in EXEMPT_PATHS


# ─── 5. LINE_MANIFEST_PATH 生效且封顶 ───────────────────────────────────────

def test_env_override_caps_the_candidate_chain(tmp_path, monkeypatch):
    """显式指路时不再往下猜：CWD 里有真清单也要以覆盖项为准。"""
    monkeypatch.setenv(wellknown.ENV_MANIFEST_PATH, str(tmp_path / "only.json"))
    (tmp_path / "only.json").write_text('{"line":"probe"}')
    assert wellknown.manifest_candidates() == [tmp_path / "only.json"]
    status, body, _ = wellknown.manifest_response()
    assert status == 200 and json.loads(body) == {"line": "probe"}


def test_candidate_chain_finds_repo_manifest_from_any_cwd(tmp_path, monkeypatch):
    """镜像里包在 site-packages、CWD 才是仓根——链必须至少命中源码树那份。"""
    monkeypatch.delenv(wellknown.ENV_MANIFEST_PATH, raising=False)
    monkeypatch.chdir(tmp_path)
    assert wellknown.manifest_path() == REPO_MANIFEST


# ─── 6. 404 / 500 分流 ──────────────────────────────────────────────────────

def test_missing_manifest_is_404_not_500(tmp_path, monkeypatch):
    monkeypatch.setenv(wellknown.ENV_MANIFEST_PATH, str(tmp_path / "nope.json"))
    status, body, headers = wellknown.manifest_response()
    assert status == 404
    assert b"not published" in body
    assert "cache-control" not in headers


def test_invalid_json_is_500_not_404(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(wellknown.ENV_MANIFEST_PATH, str(bad))
    status, body, headers = wellknown.manifest_response()
    assert status == 500
    assert b"not valid JSON" in body
    assert "cache-control" not in headers


# ─── 7. 真装配：门后面匿名可达，且门本身没被削弱 ────────────────────────────

def _asgi_with_env(monkeypatch, tmp_path, *, token):
    monkeypatch.setenv("MCP_BEARER_TOKEN", token)
    monkeypatch.setenv("SERVE_PANEL", "0")  # 本判据只测 MCP 面，别拉起 panel
    monkeypatch.setenv(
        "FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path / 'wk.db'}")
    return server._http_asgi()


@pytest.mark.parametrize("token", ["", "t0ken"])
def test_route_answers_publicly_behind_the_gate(monkeypatch, tmp_path, token):
    """配了令牌时 /mcp 401，而自描述端点仍匿名 200 且字节相同。"""
    pytest.importorskip("fastmcp")
    from starlette.testclient import TestClient

    client = TestClient(_asgi_with_env(monkeypatch, tmp_path, token=token))

    res = client.get(wellknown.WELL_KNOWN_PATH)
    assert res.status_code == 200, res.text
    assert res.content == REPO_MANIFEST.read_bytes()
    assert res.headers["cache-control"] == "public, max-age=300"
    assert res.headers["content-type"] == "application/json; charset=utf-8"

    if token:
        # 门本身没被这条豁免削弱：其余路径照样 401。
        assert client.get("/mcp").status_code == 401


def test_missing_manifest_over_http_has_no_cache_control(monkeypatch, tmp_path):
    """失败态不附 Cache-Control —— 404 别被边缘缓存成长期事实。"""
    pytest.importorskip("fastmcp")
    from starlette.testclient import TestClient

    monkeypatch.setenv(wellknown.ENV_MANIFEST_PATH, str(tmp_path / "nope.json"))
    client = TestClient(_asgi_with_env(monkeypatch, tmp_path, token="t0ken"))
    res = client.get(wellknown.WELL_KNOWN_PATH)
    assert res.status_code == 404
    assert "cache-control" not in res.headers
