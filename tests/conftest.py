"""Pytest fixtures: each test gets a fresh isolated SQLite DB."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Add project root to Python path for local imports (for smoke tests)
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


def pytest_collection_modifyitems(config, items):
    """`network` 标记的既定契约是"默认跳过，RUN_NETWORK_TESTS=1 才跑"。

    此前这条契约只实现在 `tests/real/conftest.py` 里，而带该标记的用例并不都在
    `tests/real/` 下——于是 `pytest tests` **不会**跳过它们。后果不是"多跑几条"：
    凡真实加载嵌入模型的用例（`get_model()` 首次要下载权重）在下载被阻断的机器上
    **永久挂住**，整套测试停在半途且没有任何失败输出。实测：2026-11-12 整套跑两
    次，两次都恰好停在 40%、日志 929 字节，隔离后定位到
    `TestEntityEmbeddingGenerator::test_generate_embedding`（`pytest` 退出码 124）。

    把契约放在根 conftest 里，标记语义才在整棵测试树上成立；
    `-m "not network"` 仍然有效（它在收集阶段就被 deselect，连跳过都不做）。
    """
    if os.environ.get("RUN_NETWORK_TESTS"):
        return
    skip = pytest.mark.skip(reason="network test; set RUN_NETWORK_TESTS=1 to run")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp.models import Base


@pytest.fixture
def session(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{db_path}")
    dbmod.reset_database()
    db = dbmod.get_database()
    # Test-only schema builder: connections no longer create tables (schema
    # gate), and the baseline revision is PostgreSQL-only DDL, so sqlite tests
    # build the schema explicitly from the models. create_all is equivalent to
    # the baseline here - schema_baseline.sql is its verified 2026-09-18
    # snapshot; the full migration chain is exercised on PostgreSQL.
    Base.metadata.create_all(db.engine)
    s = db.get_session()
    yield s
    s.close()
    dbmod.reset_database()
