"""挂载面 vs `clusters` 表的告警（alert-on-credential-surface-mismatch）。

为什么需要这组用例：这类问题**没有运行期症状**——派发只按 `enabled=true` 选集群，
所以"禁用集群上留着一把可用凭据"永远不会被用到，也就永远不会报错；"启用的集群缺
凭据"则要等到真正派发时才 401。2026-11-12 线上实测：前者存在了几个小时，是排查
另一件事时撞见的，不是被告知的。

钉住四件事：

1. **一致时完全安静**，且不额外发消息（它在"今天一切正常"那一侧）。
2. **缺凭据在派发之前就告警**，正文点名集群与 401 后果。
3. **没挂载时不告警、也不假装查过**（`credential_surface_checked=false`）。
4. **持续为真的不一致按天重提**（24h），不是失败告警那套 7 天。

另有一条：处置提示要按 kind 分开——孤儿/禁用是"删挂载"，缺凭据是"签发"，
给错命令等于教人用错办法。
"""

from __future__ import annotations

import datetime as dt

import pytest

from fd_open_data_mcp.models import Cluster
from fd_open_data_mcp.visibility import scan, state

DAY = 86400


@pytest.fixture
def mount(tmp_path, monkeypatch):
    """把挂载点指到 tmp 目录；返回写凭据的助手。"""
    d = tmp_path / "kubeconfigs"
    d.mkdir()
    monkeypatch.setattr(scan, "_CREDENTIALS_DIR", str(d))

    def put(*names: str) -> None:
        for n in names:
            (d / f"{n}.token").write_text("not-a-jwt", encoding="utf-8")

    put.dir = d
    return put


@pytest.fixture
def clusters(session):
    """按线上形状建表：cheap9 启用，cheap 停用，另外两个从未启用。"""
    for name, enabled in (("cheap", False), ("cheap9", True),
                          ("tencent", False), ("baidu", False)):
        session.add(Cluster(name=name, api_server=f"https://{name}:6443",
                            namespace="scraw", enabled=enabled))
    session.commit()
    return session


@pytest.fixture
def sent(monkeypatch):
    from unittest.mock import MagicMock

    box = MagicMock()
    monkeypatch.setattr(scan, "get_notifier", lambda: box)
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda *a, **k: None)
    return box


def _scan(session, now=None):
    return scan.scan_once(session, now=now or dt.datetime.now(dt.timezone.utc))


def test_consistent_surface_is_silent(clusters, mount, sent):
    """挂载与表一致 → 不产告警、不发消息、不假装查过之外的东西。"""
    mount("cheap9")
    summary = _scan(clusters)
    assert summary["credential_surface"] == []
    assert summary["credential_surface_checked"] is True
    assert sent.send.call_count == 0


def test_missing_token_alerts_before_dispatch(clusters, mount, sent):
    """启用的集群缺凭据 → 现在就说，别等派发 401。"""
    mount()  # 一个都没有
    summary = _scan(clusters)
    kinds = [(r["cluster"], r["kind"]) for r in summary["credential_surface"]]
    assert kinds == [("cheap9", "missing")]
    sent.send.assert_called_once()
    title, body = sent.send.call_args[0][:2]
    assert "CREDENTIAL SURFACE" in title
    assert "cheap9" in body and "401" in body
    # 处置是"签发"，不是"清理"
    assert "签发" in body and "rotate_worker_credential" in body


def test_disabled_cluster_with_token_alerts_and_says_delete(clusters, mount, sent):
    """离线巡检里实测到的那条线上违规（cheap 已停用却仍挂凭据）。"""
    mount("cheap", "cheap9")
    summary = _scan(clusters)
    kinds = [(r["cluster"], r["kind"]) for r in summary["credential_surface"]]
    assert kinds == [("cheap", "disabled")]
    title, body = sent.send.call_args[0][:2]
    assert "CREDENTIAL SURFACE" in title
    assert "cheap" in body and "enabled=false" in body
    # 处置是"清理挂载"，不是"去刷新"
    assert "清理" in body and "cheap.token" in body


def test_orphan_token_alerts(clusters, mount, sent):
    """表里没有这个集群，挂载里却有它的凭据。"""
    mount("cheap9", "zihan")
    summary = _scan(clusters)
    kinds = [(r["cluster"], r["kind"]) for r in summary["credential_surface"]]
    assert kinds == [("zihan", "orphan")]
    assert "孤儿凭据" in sent.send.call_args[0][1]


def test_unmounted_is_skipped_and_not_claimed_checked(clusters, monkeypatch, sent, tmp_path):
    """没挂载 = 部署形态差异，不是风险；也不能报 checked=true。"""
    monkeypatch.setattr(scan, "_CREDENTIALS_DIR", str(tmp_path / "nope"))
    summary = _scan(clusters)
    assert summary["credential_surface"] == []
    assert summary["credential_surface_checked"] is False
    assert sent.send.call_count == 0


def test_dedup_uses_24h_and_records_per_cluster_subject(clusters, mount, monkeypatch):
    """持续为真的不一致按天重提，且去重键分集群分 kind。"""
    from unittest.mock import MagicMock

    box = MagicMock()
    calls: list[tuple] = []
    monkeypatch.setattr(scan, "get_notifier", lambda: box)
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda *a, **k: calls.append((a, k)))

    mount("cheap", "cheap9")
    _scan(clusters)

    assert calls, "应当记录一次已告警"
    args, kwargs = calls[-1]
    assert args[0] == "credential-surface:cheap:disabled"
    assert args[1] == "credential-surface"
    assert kwargs.get("ttl_sec") == DAY


def test_already_alerted_is_suppressed(clusters, mount, monkeypatch):
    from unittest.mock import MagicMock

    box = MagicMock()
    monkeypatch.setattr(scan, "get_notifier", lambda: box)
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: True)
    monkeypatch.setattr(state, "mark_alerted", lambda *a, **k: None)
    mount("cheap", "cheap9")
    summary = _scan(clusters)
    assert summary["credential_surface"] == []
    assert box.send.call_count == 0


def test_surface_folds_into_one_message_with_stale(clusters, mount, sent, monkeypatch):
    """面不一致与停滞共用一次消息；标题沿用"凭据族优先于停滞"的既有次序。

    这条次序不是随便定的：到期告警本来就排在停滞之前（"之后一定不能用"比
    "现在慢"更该先被看见），面不一致属于同一族。断言它的同时把"只有一次推送"
    也钉住——两件事合成一条，是这套告警省额度的前提。
    """
    from fd_open_data_mcp.models import CrawlPolicy, PolicyRun

    p = CrawlPolicy(name="p1", enabled=True, concept_ids=[1], entity_type="fund",
                    cron_expr="0 6 * * *", timezone="UTC",
                    date_policy={"mode": "since_last"}, frequency="daily",
                    mode="per_date")
    clusters.add(p)
    clusters.commit()
    clusters.refresh(p)
    clusters.add(PolicyRun(policy_id=p.id, status="running",
                           started_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)))
    clusters.commit()

    monkeypatch.setattr(scan.snapshot, "stale_runs",
                        lambda *a, **k: [{"id": 1, "policy": "p1", "policy_id": p.id,
                                          "age_minutes": 180, "datasources": ["a"],
                                          "job_ref": "j1", "cluster_id": 1,
                                          "started_at": None}])
    monkeypatch.setattr(state, "get_scan_watermark", lambda: None)
    monkeypatch.setattr(state, "set_scan_watermark", lambda ts: None)

    mount("cheap", "cheap9")
    summary = _scan(clusters)
    assert len(summary["stale"]) == 1
    assert len(summary["credential_surface"]) == 1
    sent.send.assert_called_once()
    title, body = sent.send.call_args[0][:2]
    assert "CREDENTIAL SURFACE" in title          # 凭据族优先，与到期告警一致
    assert "stale" in body                        # 但停滞那条也在同一正文里
    assert "enabled=false" in body


def test_title_leaves_the_count_visible(clusters, mount, sent):
    """标题被截到 32 字，集群数必须还在（同到期告警那条教训）。"""
    for name in ("a", "b", "c", "d"):
        clusters.add(Cluster(name=name, api_server=f"https://{name}:6443",
                             namespace="scraw", enabled=False))
    clusters.commit()
    mount("cheap9", "a", "b", "c", "d")
    _scan(clusters)
    title, _ = sent.send.call_args[0][:2]
    assert len(title) <= 32, (len(title), title)
    assert "CREDENTIAL SURFACE" in title
