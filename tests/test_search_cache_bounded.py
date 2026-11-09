"""搜索缓存必须有上界——过期条目原来只被"忽略"，不被删除。

旧实现 TTL 只在读时判断：命中且过期就当作未命中，但**条目仍留在字典里**，只有
同一个 key 再次被请求时才会被覆盖。于是一个遍历上万实体名的 agent，会在进程生命
周期里留下上万份完整结果载荷，全部早过期——内存只涨不落，最后 OOM，而且没有任何
指标指向这里（审计 04 §11）。

修法两层：写入时先扫掉过期条目（TTL 真正生效）；再有硬上界（默认 512）按最旧淘汰
（同一 TTL 窗口内涌入大量不同 key 时，只有 cap 能兜住）。
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_open_data_mcp import search_cache as sc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.delenv("SEARCH_RESULT_CACHE_MAX", raising=False)
    monkeypatch.delenv("SEARCH_RESULT_CACHE_TTL", raising=False)
    sc.invalidate()
    yield
    sc.invalidate()


def _run(tool: str, params: dict):
    return sc.cached_search(tool, params, lambda: {"rows": [params]})


def test_fresh_hit_is_served_from_cache():
    """基本行为不许退化：第二次同参必须命中并带 cached=True。"""
    first = _run("semantic_search", {"q": "x"})
    second = _run("semantic_search", {"q": "x"})
    assert first["cached"] is False
    assert second["cached"] is True


def test_expired_entries_are_removed_not_merely_ignored(monkeypatch):
    """过期条目必须被真正删掉，而不是留在字典里当死重量。"""
    monkeypatch.setenv("SEARCH_RESULT_CACHE_TTL", "0")  # 一切立刻过期
    for i in range(50):
        _run("semantic_search", {"q": f"expired{i}"})
    # TTL=0 时每次写都会扫掉此前全部过期项，字典应只剩最后一条
    assert len(sc._store) <= 1, f"过期条目未被清除：{len(sc._store)} 条常驻"


def test_entry_count_is_capped(monkeypatch):
    """同一 TTL 窗口内涌入大量不同 key，条数不许超过上限。"""
    monkeypatch.setenv("SEARCH_RESULT_CACHE_MAX", "64")
    for i in range(500):
        _run("entity_search", {"name": f"e{i}"})
    assert len(sc._store) <= 64, f"缓存无界增长：{len(sc._store)}"


def test_eviction_drops_the_oldest_first():
    """淘汰按最旧优先——新写入的不该被自己挤掉。"""
    sc.invalidate()
    _run("semantic_search", {"q": "oldest"})
    oldest_key = sc._key("semantic_search", {"q": "oldest"})
    os.environ["SEARCH_RESULT_CACHE_MAX"] = "3"
    try:
        for i in range(20):
            _run("semantic_search", {"q": f"new{i}"})
    finally:
        os.environ.pop("SEARCH_RESULT_CACHE_MAX", None)
    assert oldest_key not in sc._store, "最旧的那条应当已被淘汰"
    assert len(sc._store) <= 3


def test_invalidate_still_clears_everything():
    _run("semantic_search", {"q": "a"})
    assert sc._store
    sc.invalidate()
    assert sc._store == {}


def test_error_payloads_are_still_not_cached():
    sc.cached_search("semantic_search", {"q": "boom"}, lambda: {"error": "nope"})
    assert sc._store == {}
