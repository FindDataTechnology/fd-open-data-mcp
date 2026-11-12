"""没跑 0021 的库：批量留存收尾必须退化，不能把写入打挂。

`data_retention_contracts` 的豁免三列（exempt_until/owner/reason）是 0021 加的。
在此之前库里只有布尔 re_fetchable——那是很长时间里的真实形态，而且爬虫集群从 PyPI
装包，镜像里的代码可能比库新、库也可能比镜像旧（滚动窗口期是双向的）。

所以"SELECT 里硬写这三列"是个会打挂整条批量写入的回归：`UndefinedColumn` 一抛，
`apply_bulk_retention` 与写入同事务，整批回滚。正确行为是退化成"没有豁免"。
"""

import pytest
from sqlalchemy import create_engine, text

from fd_open_data_mcp.fetch.retention_bulk import resolve_bulk_contracts

OLD_SCHEMA = """
CREATE TABLE concepts (
    id INTEGER PRIMARY KEY, code TEXT, entity_type TEXT, frequency TEXT,
    unit TEXT, verified INTEGER
);
CREATE TABLE data_retention_contracts (
    id INTEGER PRIMARY KEY, source TEXT, family TEXT, retention_class TEXT,
    retention_window_days INTEGER, re_fetchable INTEGER,
    upstream_history_mutable INTEGER, volume_budget_mb INTEGER
);
"""


@pytest.fixture()
def old_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'old.db'}")
    with eng.begin() as c:
        for stmt in OLD_SCHEMA.strip().split(";\n"):
            if stmt.strip():
                c.execute(text(stmt))
        c.execute(text("INSERT INTO concepts VALUES (1,'aum','fund','daily','x',1)"))
        c.execute(text(
            "INSERT INTO data_retention_contracts VALUES "
            "(1,'akshare','aum','W',1826,1,0,64)"))
    with eng.connect() as c:
        yield c


def test_resolve_works_when_exempt_columns_are_absent(old_conn):
    """缺三列时应正常返回契约，豁免视为"无"，而不是抛 UndefinedColumn。"""
    from fd_open_data_mcp.fetch.retention import exemption_state

    resolved = resolve_bulk_contracts(old_conn, source="akshare", concept_ids=[1])
    assert resolved[1]["contract"] is not None, "契约本身要读得到"
    ct = resolved[1]["contract"]
    assert ct.retention_class == "W" and ct.retention_window_days == 1826
    assert exemption_state(ct) == ("none", ""), \
        "库没这列 = 没人声明过保留，不能当成有豁免，也不能崩"
