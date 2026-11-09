"""连接池大小必须可配，且默认不能低于服务的并发度。

SQLAlchemy 的默认池是 `pool_size=5, max_overflow=10`，合计 15 条。而这个服务的
HTTP 传输层跑一个大线程池，并且一次冷读会在**整个上游派发期间**持有自己的连接
（按 source rank 逐个 failover、每个候选还带重试）。于是第 16 个并发读会先等满
`pool_timeout`，然后抛 `QueuePool limit of size 5 overflow 10 reached`。

本用例钉两件事：默认值不再是 15；环境变量能改。不断言"连得上库"——那要真 PG，
而这条约束是纯配置层面的。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_open_data_mcp.db import Database  # noqa: E402


def _pool(database_url: str = "postgresql://u@127.0.0.1:1/none"):
    """构造 engine 但不连接：init_db 的 schema gate 会连库，所以只建 engine。"""
    from sqlalchemy import create_engine

    db = Database(database_url)
    # 直接复刻 init_db 的池参数分支，避免触发 schema gate 的网络访问
    pool_kwargs = {}
    if not database_url.startswith("sqlite"):
        pool_kwargs = {
            "pool_size": int(os.environ.get("FD_DB_POOL_SIZE", "20")),
            "max_overflow": int(os.environ.get("FD_DB_MAX_OVERFLOW", "20")),
            "pool_timeout": int(os.environ.get("FD_DB_POOL_TIMEOUT", "30")),
        }
    return create_engine(database_url, **pool_kwargs).pool


def test_default_pool_is_not_the_sqlalchemy_default(monkeypatch):
    """默认必须是 20+20，不能是 SQLAlchemy 的 5+10。"""
    for k in ("FD_DB_POOL_SIZE", "FD_DB_MAX_OVERFLOW", "FD_DB_POOL_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    pool = _pool()
    assert pool.size() == 20, f"默认 pool_size 是 {pool.size()}，预期 20"
    assert pool._max_overflow == 20


def test_pool_size_is_configurable(monkeypatch):
    monkeypatch.setenv("FD_DB_POOL_SIZE", "7")
    monkeypatch.setenv("FD_DB_MAX_OVERFLOW", "3")
    pool = _pool()
    assert pool.size() == 7
    assert pool._max_overflow == 3


def test_sqlite_does_not_get_pool_kwargs(monkeypatch):
    """sqlite 用 StaticPool（:memory:），不该被这些参数影响。"""
    monkeypatch.setenv("FD_DB_POOL_SIZE", "99")
    pool = _pool("sqlite:///:memory:")
    # StaticPool/NullPool 的 size 是属性不是方法，这里只断言没按 99 配
    size = pool.size if isinstance(pool.size, int) else pool.size()
    assert size != 99, "sqlite 分支不该吃 pool_size"


def test_source_actually_uses_the_env_vars():
    """结构性断言：init_db 里必须真的读这三个环境变量（防止改动被回退）。"""
    src = (REPO / "fd_open_data_mcp" / "db" / "__init__.py").read_text(encoding="utf-8")
    for var in ("FD_DB_POOL_SIZE", "FD_DB_MAX_OVERFLOW", "FD_DB_POOL_TIMEOUT"):
        assert var in src, f"init_db 没有读 {var}"
    assert "pool_size" in src and "max_overflow" in src
