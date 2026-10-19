"""错误语义契约（mcp-error-semantics 组 4/4.5）。

本服务（71 工具）既有显式错误载荷（`{"error": kind, "detail": ...}`），
也有**部分降级**：语义检索合并本地命中与 registry 语料，registry 取不到时
本地结果仍然有效。

后者是本文件要钉住的：`[]` 单独出现时，"registry 没贡献"与"registry 取不到"
看起来一模一样——前者是完整答案，后者是部分答案。现在降级会在响应里带
`degraded: true` 与 `degraded_reason`，调用方能区分。
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.semantic import registry_search as rs


def test_degraded_reason_is_consumed_not_just_read():
    """降级原因必须"取走"：只读不清会让上一次请求的降级泄漏到下一次成功请求。"""
    rs._set_degraded("registry_corpus_unreadable: OperationalError")
    assert rs.take_degraded_reason() == "registry_corpus_unreadable: OperationalError"
    assert rs.take_degraded_reason() is None, "第二次取必须是 None（已消费）"


def test_no_degradation_yields_none():
    rs.take_degraded_reason()  # 清空
    assert rs.take_degraded_reason() is None


def test_search_registry_signals_degradation_on_missing_corpus(monkeypatch):
    """语料表不存在时返回空列表**并**留下降级原因。"""
    rs.take_degraded_reason()

    class FakeSession:
        def close(self):
            pass

    monkeypatch.setattr(rs, "tables_present", lambda s: False)
    out = rs.search_registry("茅台", 10, session=FakeSession())
    assert out == []
    assert rs.take_degraded_reason() == "registry_corpus_absent"


def test_search_registry_signals_degradation_on_unreadable_corpus(monkeypatch):
    from sqlalchemy.exc import OperationalError

    rs.take_degraded_reason()

    class FakeSession:
        def close(self):
            pass

    def boom():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(rs, "tables_present", lambda s: True)
    monkeypatch.setattr(rs, "_backend", boom)
    out = rs.search_registry("茅台", 10, session=FakeSession())
    assert out == []
    reason = rs.take_degraded_reason()
    assert reason and "unreadable" in reason


def test_a_successful_registry_search_leaves_no_degradation(monkeypatch):
    """成功的检索不能留下上一次的降级原因。"""
    rs._set_degraded("stale")
    rs.take_degraded_reason()  # 模拟上一次请求消费掉

    class FakeSession:
        def close(self):
            pass

    monkeypatch.setattr(rs, "tables_present", lambda s: True)
    monkeypatch.setattr(rs, "_backend", lambda: "json")
    monkeypatch.setattr(rs, "_search_json", lambda s, q, l: [{"semantic_code": "x"}])
    out = rs.search_registry("茅台", 10, session=FakeSession())
    assert out == [{"semantic_code": "x"}]
    assert rs.take_degraded_reason() is None
