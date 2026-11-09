"""A missing cluster id must not abort the whole seed (FK violation)."""
from fd_open_data_mcp.models import Cluster, Proxy
from fd_open_data_mcp.proxy import seed


def test_missing_cluster_seeds_without_tag(session):
    # cluster id 3 is absent from this table on purpose
    out = seed.register_egress_pod(session, "aliyun", "47.99.94.85", 30080,
                                   "u:p", cluster_id=3)
    session.commit()
    assert out == "created"
    row = session.query(Proxy).filter_by(label="gost-aliyun").one()
    assert row.cluster_id is None


def test_existing_cluster_keeps_the_tag(session):
    c = Cluster(name="tencent", api_server="https://x:6443", namespace="scraw",
                image="img", tags=[], capacity=1, enabled=True)
    session.add(c); session.commit()
    seed.register_egress_pod(session, "tencent", "1.2.3.4", 30080, "u:p",
                             cluster_id=c.id)
    session.commit()
    row = session.query(Proxy).filter_by(label="gost-tencent").one()
    assert row.cluster_id == c.id
