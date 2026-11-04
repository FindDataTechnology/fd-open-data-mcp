"""内圈商家授权闸（add-crossborder-vertical-line 3.2，spec crossborder-mcp-surface
「内圈数据只经商家授权连接器接入」的工具面实现）。

验收口径：未授权查询返回结构化「不可得（未授权）」——绝不报错、绝不静默空值；
授权经由 FD_CROSSBORDER_MERCHANT_AUTH_JSON 登记后同形翻转为 authorized。
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.annotations import TOOL_ANNOTATIONS
from fd_open_data_mcp.server import crossborder_merchant_status


def test_unauthorized_returns_structured_unavailable(monkeypatch):
    """默认（无授权登记）→ 结构化「不可得（未授权）」，不抛错、非空值冒充。"""
    monkeypatch.delenv("FD_CROSSBORDER_MERCHANT_AUTH_JSON", raising=False)
    r = crossborder_merchant_status("tiktok_shop")
    assert r["status"] == "unavailable" and r["reason"] == "unauthorized"
    assert r["platform"] == "tiktok_shop" and r["first_channel"] is True
    assert "未授权" in r["detail"] and "how_to_authorize" in r


def test_unknown_platform_is_structured_not_raised():
    r = crossborder_merchant_status("not_a_platform")
    assert r["status"] == "unknown_platform" and "tiktok_shop" in r["known_platforms"]


def test_authorized_flips_same_shape(monkeypatch):
    """授权登记后同形翻转 authorized——下游无需改报数逻辑。"""
    monkeypatch.setenv("FD_CROSSBORDER_MERCHANT_AUTH_JSON",
                       '{"tiktok_shop": {"authorized": true, "merchant_id": "m1", "scopes": ["read"]}}')
    r = crossborder_merchant_status("tiktok_shop")
    assert r["status"] == "authorized" and r["merchant_id"] == "m1" and r["scopes"] == ["read"]
    assert set(r) >= {"status", "platform"}  # 同形：status/platform 键恒在


def test_tool_is_declared_read_only():
    """运行时注解契约：新工具必须在 TOOL_ANNOTATIONS 在册（缺声明即 KeyError）。"""
    assert TOOL_ANNOTATIONS["crossborder_merchant_status"]["readOnlyHint"] is True
